"""M8 入口：真实日值导入、更正发布、版本对看与版本跟随的月报下载。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import Response

from ... import errors
from ...web import Ctx, get_ctx, render
from .service import CorrectionService
from .store import CorrectionStore

router = APIRouter(tags=["M8 日值更正发布"])


def _service(ctx: Ctx) -> CorrectionService:
    return CorrectionService(ctx.settings, CorrectionStore(ctx.settings.db_path))


def _records_to_csv(records: list[dict]) -> bytes:
    """页面手工粘贴的 JSON 记录统一转成规范 CSV，复用同一条解析链路。"""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["station", "date", "rainfall_mm", "version"])
    for row in records:
        writer.writerow([
            row.get("station", ""), row.get("date", ""),
            "" if row.get("rainfall_mm") is None else row.get("rainfall_mm"),
            row.get("version", 1)])
    return buf.getvalue().encode("utf-8")


@router.post("/api/corrections/stage")
async def api_stage(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload: UploadFile = form.get("file")
        if upload is None:
            raise errors.BadRequest("缺少上传文件", code="E_BAD_REQUEST")
        data = await upload.read()
        source_name = upload.filename or "upload.csv"
        reason = (form.get("reason") or "").strip()
        base_gen = form.get("base_gen") or None
    else:
        body = await request.json()
        records = body.get("records")
        if not isinstance(records, list) or not records:
            raise errors.BadRequest("records 必须是非空数组", code="E_BAD_REQUEST")
        data = _records_to_csv(records)
        source_name = body.get("source_name") or "api-records"
        reason = (body.get("reason") or "").strip()
        base_gen = body.get("base_gen") or None
    return _service(ctx).stage(data=data, source_name=source_name, reason=reason,
                               expected_base_gen=base_gen)


@router.post("/api/corrections/{run_id}/publish")
def api_publish(run_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return _service(ctx).publish(run_id)


@router.post("/api/corrections/{run_id}/rebase")
def api_rebase(run_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return _service(ctx).rebase_run(run_id)


@router.get("/api/corrections/runs")
def api_runs(limit: int = 20, ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"runs": _service(ctx).store.list_runs(limit=limit)}


@router.get("/api/corrections/generations")
def api_generations(ctx: Ctx = Depends(get_ctx)) -> dict:
    svc = _service(ctx)
    return {"current_gen": svc.store.current_gen(),
            "generations": svc.store.list_publishes()}


@router.post("/api/corrections/generations/{gen_id}/switch")
def api_switch(gen_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return _service(ctx).switch_gen(gen_id)


@router.get("/api/corrections/view")
def api_view(gen_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    svc = _service(ctx)
    view = svc.gen_view(gen_id)
    if view is None:
        return {"gen_id": gen_id or svc.store.current_gen(), "monthly": [],
                "days": [], "conflicts": [], "gen": None}
    return {
        "gen_id": view["gen"]["gen_id"],
        "gen": view["gen"],
        "monthly": [{**m, "rainfall_mm": str(m["rainfall_mm"])}
                    for m in view["monthly"]],
        "days": [{**d, "value_norm": (None if d["value_norm"] is None
                                      else str(d["value_norm"]))}
                 for d in view["days"]],
        "conflicts": view["conflicts"],
        "reconcile": svc.current_reconcile(),
    }


@router.get("/api/corrections/compare")
def api_compare(left: str, right: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return _service(ctx).compare_gens(left, right)


@router.get("/api/corrections/reconcile")
def api_correction_reconcile(ctx: Ctx = Depends(get_ctx)) -> dict:
    return _service(ctx).current_reconcile()


@router.get("/api/corrections/monthly.csv")
def export_correction_monthly(gen_id: str | None = None,
                              ctx: Ctx = Depends(get_ctx)) -> Response:
    """月报下载跟随所选版本：不传 gen_id 即当前世代。"""
    svc = _service(ctx)
    target = gen_id or svc.store.current_gen()
    if target is None:
        raise errors.NotFound("还没有已发布的更正版本可导出",
                              code="E_CORR_RUN_NOT_FOUND")
    view = svc.gen_view(target)
    if view is None:
        raise errors.NotFound("所选版本不存在，仍可读取旧版",
                              code="E_CORR_RUN_NOT_FOUND")
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["gen_id", "station", "month", "rainfall_mm", "expected_days",
                     "observed_days", "zero_days", "missing_days", "isolated_days",
                     "missing_dates", "state", "reason"])
    reason = view["gen"]["reason"]
    for m in view["monthly"]:
        writer.writerow([target, m["station"], m["month"], m["rainfall_mm"],
                         m["expected_days"], m["observed_days"], m["zero_days"],
                         m["missing_days"], m["isolated_days"],
                         "|".join(m["missing_dates"]), m["state"], reason])
    filename = f"correction-monthly-{target}.csv"
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/corrections")
def page_corrections(request: Request, gen_id: str | None = None,
                     ctx: Ctx = Depends(get_ctx)):
    svc = _service(ctx)
    current = svc.store.current_gen()
    selected = gen_id or current
    view = svc.gen_view(selected) if selected else None
    generations = svc.store.list_publishes()
    runs = svc.store.list_runs()
    reconcile = svc.current_reconcile()
    compare_pair = None
    if len(generations) >= 2:
        compare_pair = svc.compare_gens(generations[-2]["gen_id"],
                                        generations[-1]["gen_id"])
    return render(request, "corrections.html", page="corrections",
                  page_title="月量更正", view=view, generations=generations,
                  runs=runs, reconcile=reconcile, current_gen=current,
                  selected_gen=selected, compare_pair=compare_pair)
