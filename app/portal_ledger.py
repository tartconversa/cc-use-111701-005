"""门户采集收据：把门店、日期、条目与每一次图片/接口响应连成可核验证据链。

与单响应版 :mod:`app.ledger` 相同，收据只含响应摘要而非登录凭据：
所有凭据类响应头在此处统一脱敏，调用方无需各自记得处理。
"""
from __future__ import annotations

from typing import Optional

from . import checkpoint as cp
from .portal_driver import SENSITIVE_HEADERS, Detail, ListEntry, Selection
from .source import Response


def sanitize_headers(headers) -> tuple[tuple[str, str], ...]:
    """剥除凭据类响应头；其余头保留原值用于核验。"""
    return tuple((k, '***REDACTED***') if k.lower() in SENSITIVE_HEADERS
                 else (k, v) for k, v in headers)


def response_ref(response: Optional[Response]) -> Optional[dict]:
    """响应引用：身份、地址、状态与实体摘要；response 为 None 时为 None。"""
    if response is None:
        return None
    return {
        'source_id': response.source_id,
        'url': response.url,
        'status': response.status,
        'headers': list(sanitize_headers(response.headers)),
        'body_sha256': response.sha256,
        'body_bytes': len(response.body),
    }


def selection_ref(selection: Selection) -> dict:
    return {
        'region': selection.region,
        'store': selection.store,
        'day': selection.day,
        'fingerprint': selection.fingerprint(),
    }


def entry_ref(entry: ListEntry) -> dict:
    return {
        'audit_id': entry.audit_id,
        'store': entry.store,
        'day': entry.day,
        'media_id': entry.media_id,
    }


def build_run_receipt(*, run_id: str, selection: Selection,
                      checkpoint: cp.Checkpoint,
                      page_refs: list[dict],
                      item_records: list[dict],
                      gaps: list[dict]) -> dict:
    """组装一次运行（可能跨多次续取）的完整收据。"""
    return {
        'schema': 'portal-collection-receipt/v1',
        'run_id': run_id,
        'selection': selection_ref(selection),
        'status': checkpoint.status,
        'stop_reason': checkpoint.stop_reason,
        'progress': {
            'pages_visited': checkpoint.pages_visited,
            'entries_seen': checkpoint.entries_seen,
            'delivered_count': len(checkpoint.delivered_media),
            'gap_count': len(gaps),
        },
        'response_chain': {
            'list_pages': page_refs,
        },
        'items': item_records,
        'gaps': gaps,
        'integrity': _integrity(page_refs, item_records, gaps),
    }


def item_record(entry: ListEntry, detail: Detail,
                image_response: Optional[Response]) -> dict:
    """单个条目的证据链：列表身份→详情响应→原图响应。"""
    record = {
        'entry': entry_ref(entry),
        'detail_response': response_ref(detail.detail_response),
        'image_response': response_ref(image_response),
    }
    if image_response is not None:
        record['media_id'] = detail.media_id
        record['image_sha256'] = image_response.sha256
        # 身份绑定断言，便于独立核验：原图必须挂在同一条目上。
        record['identity_binding'] = {
            'audit_id': entry.audit_id,
            'media_id': detail.media_id,
            'bound': entry.audit_id == detail.audit_id
                     and entry.media_id == detail.media_id,
        }
    return record


def _integrity(page_refs: list[dict], item_records: list[dict],
               gaps: list[dict]) -> dict:
    delivered = [r for r in item_records if r.get('image_response')]
    sha_list = [r['image_response']['body_sha256'] for r in delivered]
    return {
        'list_page_count': len(page_refs),
        'delivered_count': len(delivered),
        'gap_count': len(gaps),
        'unique_image_sha256': len(set(sha_list)),
        'all_images_distinct': len(sha_list) == len(set(sha_list)),
        'all_bindings_bound': all(
            r.get('identity_binding', {}).get('bound', False)
            for r in delivered),
        'complete': not gaps and all(
            r.get('identity_binding', {}).get('bound', False)
            for r in delivered),
    }


def verify_receipt(receipt: dict) -> dict:
    """独立核验收据：身份绑定、去重、缺口与状态一致性。"""
    items = receipt.get('items', [])
    delivered = [r for r in items if r.get('image_response')]
    sha_list = [r['image_response']['body_sha256'] for r in delivered]
    media_list = [r['media_id'] for r in delivered]
    problems: list[str] = []
    if len(sha_list) != len(set(sha_list)):
        problems.append('存在重复的原图内容摘要')
    if len(media_list) != len(set(media_list)):
        problems.append('存在重复交付的 media_id')
    if any(not r.get('identity_binding', {}).get('bound') for r in delivered):
        problems.append('存在条目与原图身份未绑定的记录')
    # 凭据头不得出现在收据中：逐处检查凭据类响应头必须已脱敏。
    refs = [*(r.get('detail_response') or {} for r in items),
            *(r.get('image_response') or {} for r in items),
            *receipt.get('response_chain', {}).get('list_pages', [])]
    for ref in refs:
        for key, value in (ref or {}).get('headers', []):
            if key.lower() in SENSITIVE_HEADERS and value != '***REDACTED***':
                problems.append(f'凭据头未脱敏: {key}')
    return {
        'ok': not problems,
        'problems': problems,
        'delivered': len(delivered),
        'gaps': len(receipt.get('gaps', [])),
        'status': receipt.get('status'),
    }
