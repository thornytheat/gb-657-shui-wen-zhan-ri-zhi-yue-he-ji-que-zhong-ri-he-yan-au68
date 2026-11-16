"""更正发布编排：暂存 → 裁决 → 发布世代 → 切换当前版本，全程可续跑。

并发与一致性：
- 发布走 BEGIN IMMEDIATE 单事务，配合 corr_state 里的发布锁，并发请求一个进、
  其余直接 409（E_CORR_BUSY），不会产生两个世代；
- 过期更正：暂存时记录 base_gen，发布时复核当前世代，已被别人推进则 409
  （E_CORR_STALE），本任务保持 staged，可显式改基准后重试；
- 中断续跑：阶段进度落在 corr_runs.checkpoints_json，重入先清理未激活的半成品
  世代再重算，原始日值只追加不重插，因此重试不重复生成月报；
- 切换失败不删旧世代：指针没改成就读旧版，任何已发布世代都可随时回看。
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from ... import config, errors, manifest
from . import engine
from .parser import parse
from .store import LOCK_KEY, CorrectionStore

GEN_PREFIX = "gen"
RUN_PREFIX = "CR"


def _now() -> dt.datetime:
    return dt.datetime.now(config.UTC)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(config.UTC).isoformat()


def _gen_id(run_id: str, stamp: str) -> str:
    return f"{GEN_PREFIX}-{stamp.strftime('%Y%m%d%H%M%S')}-{run_id[-4:]}"


def _run_id(stamp: dt.datetime) -> str:
    return manifest.new_run_id(RUN_PREFIX)


class CorrectionService:
    # 进程级：每个库只在首次构造时清理一次崩溃残留锁，之后不再动锁
    _lock_repaired_paths: set[str] = set()

    def __init__(self, settings, store: CorrectionStore):
        self.settings = settings
        self.store = store
        resolved = str(Path(store.path).resolve())
        if resolved not in self._lock_repaired_paths:
            store.reset_stale_lock()
            self._lock_repaired_paths.add(resolved)

    # ---------- 1. 导入真实日值，暂存待发布 ----------
    def stage(self, *, data: bytes, source_name: str, reason: str,
              expected_base_gen: str | None = None) -> dict:
        reason = (reason or "").strip()
        if not reason:
            raise errors.BadRequest("更正理由必填，原始行与理由要一并留痕",
                                    code="E_BAD_REQUEST")
        parsed = parse(data)
        if parsed.file_error:
            raise errors.UnprocessableInput(
                f"更正文件无法采信：{parsed.file_error}",
                code=parsed.file_error,
                detail={"failed_stage": "parse", "header": parsed.header})

        digest = manifest.digest_bytes(data)
        duplicate = self.store.find_run_by_digest(digest)
        if duplicate is not None and duplicate["status"] in ("staged", "done"):
            # 同一份文件不重复建任务、不重复插行，直接回指既有任务
            return self._run_view(duplicate, replayed=True)

        stamp = _now()
        run = {
            "run_id": _run_id(stamp),
            "source_name": source_name or "upload",
            "source_digest": digest,
            "reason": reason,
            "base_gen": expected_base_gen or self.store.current_gen(),
            "status": "staged",
            "input_count": parsed.input_count,
            "checkpoints": [],
            "note": f"{parsed.encoding} 解码" + ("（回退）" if parsed.fallback else ""),
            "created_at": _iso(stamp),
            "updated_at": _iso(stamp),
        }
        daily = []
        for row in parsed.rows + parsed.isolated:
            daily.append({
                "run_id": run["run_id"], "lineno": row.lineno,
                "station": row.station, "date": row.date,
                "version": row.version, "value_text": row.value_text,
                "value_norm": row.value_norm, "kind": row.kind,
                "row_status": row.row_status, "reason_code": row.reason_code,
                "note": row.note,
            })
        with self.store.connect() as conn:
            self.store.insert_run(conn, run)
            self.store.insert_daily(conn, daily, run["created_at"])
        return self._run_view(self.store.get_run(run["run_id"]), replayed=False)

    # ---------- 2. 发布：锁内裁决、冻结世代、切换指针 ----------
    def publish(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if run is None:
            raise errors.NotFound(f"更正任务 {run_id} 不存在",
                                  code="E_CORR_RUN_NOT_FOUND")
        if run["status"] == "done":
            raise errors.Conflict("该更正已发布，禁止重复生成月报",
                                  code="E_CORR_RUN_DONE")
        if run["status"] != "staged":
            raise errors.Conflict("该更正任务不是待发布状态",
                                  code="E_CORR_RUN_NOT_STAGED")

        try:
            with self.store.immediate() as conn:
                if conn.execute("SELECT value FROM corr_state WHERE key=?",
                                (LOCK_KEY,)).fetchone():
                    raise errors.Conflict("另一更正正在发布，请稍后再试",
                                          code="E_CORR_BUSY")
                current = conn.execute(
                    "SELECT value FROM corr_state WHERE key='current_gen'"
                ).fetchone()
                current_gen = current["value"] if current else None
                # 过期裁决：基准世代已被推进 → 拒绝，旧版月报保持原样
                if run["base_gen"] != current_gen:
                    raise errors.Conflict(
                        "过期更正：依据版本已不是当前版，请重新基于当前版导入",
                        code="E_CORR_STALE",
                        detail={"base_gen": run["base_gen"],
                                "current_gen": current_gen})
                self.store.set_state(conn, LOCK_KEY, run_id)
        except errors.MeterbackError:
            raise

        resumed = bool(run["checkpoints"])
        try:
            result = self._publish_locked(run, resumed)
        except Exception:
            # 崩溃/异常都释放锁；半成品世代在下次进入时清理，可续跑
            self._unlock()
            raise
        self._unlock()
        return result

    def _publish_locked(self, run: dict, resumed: bool) -> dict:
        stamp = _now()
        # 续跑复用上一轮的半成品世代号；新一轮才生成，保证重试不重复出月报
        gen_id = run.get("pending_gen") or _gen_id(run["run_id"], stamp)
        checkpoints = list(run["checkpoints"])

        with self.store.immediate() as conn:
            # 续跑：清掉上次未激活的半成品（绝不会是当前世代），原始行保留
            if resumed and run.get("pending_gen"):
                self.store.delete_gen_products(conn, gen_id)
            conn.execute("UPDATE corr_runs SET pending_gen=? WHERE run_id=?",
                         (gen_id, run["run_id"]))
            self._checkpoint(conn, run["run_id"], checkpoints, "arbitrate")

            all_rows = self.store.load_all_valid_daily(conn)
            decision = engine.arbitrate(all_rows)

            conflict_row_ids = {r["row_id"] for c in decision["conflicts"]
                                for r in c["rows"]}
            # 同版异值：把组内行落为隔离状态（原始行仍在，状态可追溯）
            for row_id in conflict_row_ids:
                conn.execute(
                    "UPDATE corr_daily_values SET row_status='isolated', "
                    "reason_code='Q_CORR_VERSION_CONFLICT' WHERE row_id=?",
                    (row_id,))

            self._checkpoint(conn, run["run_id"], checkpoints, "freeze")
            monthly = engine.aggregate(decision["selected"], decision["conflicts"])
            self.store.insert_publish(conn, {
                "gen_id": gen_id, "run_id": run["run_id"],
                "base_gen": run["base_gen"], "reason": run["reason"],
                "source_digest": run["source_digest"], "status": "frozen",
                "created_at": _iso(stamp),
            })
            self.store.insert_day_versions(conn, gen_id, [{
                "station": r["station"], "date": r["date"],
                "version": r["version"], "kind": r["kind"],
                "value_norm": r["value_norm"], "source_row_id": r["row_id"],
                "source_run_id": r["run_id"],
            } for r in decision["selected"]])
            self.store.insert_conflicts(conn, gen_id, decision["conflicts"])
            self.store.insert_monthly(conn, gen_id, monthly)

            self._checkpoint(conn, run["run_id"], checkpoints, "switch")
            # 切换只改指针；若这一步失败，旧指针与旧世代完好，仍可读旧版
            self.store.set_state(conn, "current_gen", gen_id)

            counts = self._counts(conn, run["run_id"], decision)
            conn.execute(
                "UPDATE corr_runs SET status='done', published_at=?, updated_at=?, "
                "accepted_count=?, duplicate_count=?, isolated_count=?, "
                "zero_count=?, missing_count=?, conflict_count=?, "
                "checkpoints_json=?, pending_gen=NULL WHERE run_id=?",
                (_iso(stamp), _iso(stamp), counts["accepted"], counts["duplicates"],
                 counts["isolated"], counts["zero"], counts["missing"],
                 len(decision["conflicts"]), json.dumps([], ensure_ascii=False),
                 run["run_id"]))
        return {"status": "done", "gen_id": gen_id, "run_id": run["run_id"],
                "resumed": resumed, "counts": counts,
                "monthly_rows": len(monthly)}

    def _checkpoint(self, conn, run_id: str, checkpoints: list[str], stage: str) -> None:
        if stage not in checkpoints:
            checkpoints.append(stage)
        conn.execute("UPDATE corr_runs SET checkpoints_json=?, updated_at=? WHERE run_id=?",
                     (json.dumps(checkpoints, ensure_ascii=False), _iso(_now()), run_id))

    def _unlock(self) -> None:
        with self.store.connect() as conn:
            self.store.set_state(conn, LOCK_KEY, None)

    def _counts(self, conn, run_id: str, decision: dict) -> dict:
        # 本次任务的四账（只数这次导入的行），保证 输入 = 采纳 + 重复 + 隔离。
        # 隔离身份统一以落库后的 row_status 为准（坏行 + 同版异值整组）。
        rows = conn.execute(
            "SELECT * FROM corr_daily_values WHERE run_id=?", (run_id,)
        ).fetchall()
        source = len(rows)
        accepted_ids = {r["row_id"] for r in decision["selected"]}
        accepted = sum(1 for r in rows if r["row_id"] in accepted_ids)
        isolated = sum(1 for r in rows if r["row_status"] == "isolated")
        duplicates = source - accepted - isolated
        zero = sum(1 for r in rows if r["kind"] == "measured"
                   and r["value_norm"] is not None and str(r["value_norm"]) == "0")
        missing = sum(1 for r in rows if r["kind"] == "missing")
        accounts = engine.reconcile(source, accepted, duplicates, isolated)
        return {"source": source, "accepted": accepted, "duplicates": duplicates,
                "isolated": isolated, "zero": zero, "missing": missing,
                "balanced": accounts["balanced"], "delta": accounts["delta"]}

    # ---------- 3. 回看：任意世代的月量、日值、对账 ----------
    def gen_view(self, gen_id: str | None = None) -> dict | None:
        gen_id = gen_id or self.store.current_gen()
        if gen_id is None:
            return None
        publish = self.store.get_publish(gen_id)
        if publish is None:
            return None
        monthly = self.store.load_monthly(gen_id)
        days = self.store.load_day_versions(gen_id)
        conflicts = self.store.load_conflicts(gen_id)
        return {"gen": publish, "monthly": monthly, "days": days,
                "conflicts": conflicts}

    def compare_gens(self, left_gen: str, right_gen: str) -> dict:
        """前后版本对看：同站同月并排放，标出变化量。"""
        left = { (m["station"], m["month"]): m for m in self.store.load_monthly(left_gen) }
        right = { (m["station"], m["month"]): m for m in self.store.load_monthly(right_gen) }
        keys = sorted(set(left) | set(right))
        rows = []
        for key in keys:
            a, b = left.get(key), right.get(key)
            av = a["rainfall_mm"] if a else None
            bv = b["rainfall_mm"] if b else None
            delta = None
            if av is not None and bv is not None:
                delta = (bv - av).quantize(engine.QUANT)
            rows.append({"station": key[0], "month": key[1],
                         "left": None if av is None else str(av),
                         "right": None if bv is None else str(bv),
                         "delta": None if delta is None else str(delta),
                         "changed": delta is not None and delta != Decimal("0.0")})
        return {"left_gen": left_gen, "right_gen": right_gen, "rows": rows}

    def current_reconcile(self) -> dict:
        """全量四账：跨所有更正任务，输入 = 采纳 + 重复 + 隔离。"""
        with self.store.connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) n FROM corr_daily_values").fetchone()["n"]
            isolated = conn.execute(
                "SELECT COUNT(*) n FROM corr_daily_values WHERE row_status='isolated'"
            ).fetchone()["n"]
        gen_id = self.store.current_gen()
        accepted = len(self.store.load_day_versions(gen_id)) if gen_id else 0
        duplicates = total - accepted - isolated
        accounts = engine.reconcile(total, accepted, duplicates, isolated)
        return {"gen_id": gen_id, "total_source": total, **accounts}

    def switch_gen(self, gen_id: str) -> dict:
        """切换当前查看/导出的世代。目标世代必须已冻结；指针没改成不影响旧版。"""
        target = self.store.get_publish(gen_id)
        if target is None or target["status"] != "frozen":
            raise errors.NotFound("目标版本不存在或尚未冻结，仍可继续读取旧版",
                                  code="E_CORR_RUN_NOT_FOUND")
        previous = self.store.current_gen()
        with self.store.immediate() as conn:
            if conn.execute("SELECT value FROM corr_state WHERE key=?",
                            (LOCK_KEY,)).fetchone():
                raise errors.Conflict("另一更正正在发布，请稍后再试",
                                      code="E_CORR_BUSY")
            self.store.set_state(conn, "current_gen", gen_id)
        return {"previous_gen": previous, "current_gen": gen_id}

    def rebase_run(self, run_id: str) -> dict:
        """过期更正改挂当前世代，再发布即可，无需重新导入原始行。"""
        run = self.store.get_run(run_id)
        if run is None:
            raise errors.NotFound(f"更正任务 {run_id} 不存在",
                                  code="E_CORR_RUN_NOT_FOUND")
        if run["status"] != "staged":
            raise errors.Conflict("只有待发布任务可以重新定基",
                                  code="E_CORR_RUN_NOT_STAGED")
        current = self.store.current_gen()
        self.store.update_run(run_id, base_gen=current, updated_at=_iso(_now()),
                              checkpoints=[], pending_gen=None)
        return {"run_id": run_id, "base_gen": current}

    def _run_view(self, run: dict, *, replayed: bool) -> dict:
        return {"status": run["status"], "run_id": run["run_id"],
                "base_gen": run["base_gen"], "reason": run["reason"],
                "input_count": run["input_count"], "replayed": replayed,
                "source_digest": run["source_digest"],
                "created_at": run["created_at"]}
