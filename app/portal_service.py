"""本地演练入口：用内存模拟门户跑一遍可恢复采集并落盘收据。

真实运行时由适配层把 :class:`~app.portal_driver.BrowserPort` 换成
Playwright/Selenium 实现，编排与检查点逻辑不变。本入口与全部样例数据均为
合成数据，不接触真实企业系统，也不读取或保存任何登录凭据。

用法::

    python -m app.portal_service <输出目录>            # 完整跑一遍
    python -m app.portal_service <输出目录> --expire-after 2  # 第2条后过期
    python -m app.portal_service <输出目录> --resume          # 重新登录后续取
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import checkpoint as cp
from . import portal_ledger as ledger
from .collector import CollectionAborted, PortalCollector
from .portal_driver import Selection, SimulatedPortal

REGION = 'east-1'
STORE = 'store-7'
DAY = '2026-11-15'


def build_demo_portal(*, expire_after: int | None = None,
                      page_size: int = 2) -> SimulatedPortal:
    """构造含 5 条记录（跨 3 页）、其中 1 条原图缺失的合成门户。"""
    rows = []
    for idx in range(5):
        rows.append({
            'audit_id': f'a-{idx}',
            'media_id': f'm-{idx}',
            'image_url': f'https://portal.test/media/m-{idx}.jpg',
            'image': bytes([0x10 + idx]) * 16,
        })
    dataset = {REGION: {STORE: {DAY: rows}}}
    portal = SimulatedPortal(dataset, page_size=page_size, detail_load_polls=2)
    portal.unavailable_media = {'m-3'}  # 明确缺口
    if expire_after is not None:
        portal.expire_after = expire_after  # 由测试/钩子控制
        original_fetch = portal.fetch_original

        def fetch_then_maybe_expire(detail):
            result = original_fetch(detail)
            if len(portal.served_media) >= expire_after:
                portal.fetch_original = original_fetch  # 仅触发一次
                portal.expire_session()
            return result

        portal.fetch_original = fetch_then_maybe_expire  # type: ignore[assignment]
    return portal


def demo_selection() -> Selection:
    return Selection(REGION, STORE, DAY)


def run(output_dir: str, *, resume: bool = False,
        expire_after: int | None = None) -> dict:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out / 'checkpoint.json'

    # 续取重建门户时，按检查点的停止状态决定会话是否仍处于过期。
    was_stopped = False
    if resume and checkpoint_path.exists():
        was_stopped = cp.load(checkpoint_path).status == cp.STATUS_STOPPED
    portal = build_demo_portal(expire_after=None if resume else expire_after)
    if was_stopped and not resume:
        portal.expire_session()
    if resume:
        # 模拟重新登录：会话恢复，但列表状态必须重新应用筛选后才可信。
        portal.relogin()
    collector = PortalCollector(portal)

    try:
        result = collector.run(demo_selection(), str(checkpoint_path))
    except CollectionAborted as aborted:
        state = {
            'status': aborted.checkpoint.status,
            'stop_reason': aborted.reason,
            'resumable': True,
            'checkpoint': str(checkpoint_path),
            'hint': '重新登录后带 --resume 运行；续取会先重确认筛选与条目身份',
        }
        (out / 'stopped.json').write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        return state

    receipt_path = out / 'receipt.json'
    receipt_path.write_text(
        json.dumps(result.receipt, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    verification = ledger.verify_receipt(result.receipt)
    (out / 'verify.json').write_text(
        json.dumps(verification, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    return {'status': result.receipt['status'],
            'resumed': result.resumed,
            'verification': verification,
            'receipt': str(receipt_path)}


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    resume = '--resume' in argv
    argv = [a for a in argv if a != '--resume']
    expire_after = None
    if '--expire-after' in argv:
        pos = argv.index('--expire-after')
        expire_after = int(argv[pos + 1])
        del argv[pos:pos + 2]
    if len(argv) != 1:
        raise SystemExit('用法: python -m app.portal_service <输出目录> '
                         '[--expire-after N] [--resume]')
    print(json.dumps(run(argv[0], resume=resume,
                         expire_after=expire_after),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
