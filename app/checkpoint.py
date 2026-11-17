"""可恢复采集的持久化检查点。

检查点只记录"可解释的位置"与身份信息：筛选指纹、翻页句柄、当前条目、
已交付 media、明确缺口。它不保存任何登录凭据，写入使用临时文件+原子替换，
避免会话中断时留下半截文件。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 检查点结构版本，恢复时用于识别不兼容的历史格式。
CHECKPOINT_VERSION = 1

STATUS_RUNNING = 'running'
STATUS_STOPPED = 'stopped_session_expired'
STATUS_COMPLETE = 'complete'


@dataclass
class Checkpoint:
    run_id: str
    selection_fingerprint: str
    selection: dict
    cursor: Optional[str] = None
    page_index: int = 0
    current_audit_id: Optional[str] = None
    delivered_media: list[str] = field(default_factory=list)
    delivered_sha: list[str] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    pages_visited: int = 0
    entries_seen: int = 0
    # 脱敏后的响应引用与条目证据，跨续取累积，使最终收据覆盖整个 run。
    page_refs: list[dict] = field(default_factory=list)
    item_records: list[dict] = field(default_factory=list)
    status: str = STATUS_RUNNING
    stop_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'version': CHECKPOINT_VERSION,
            'run_id': self.run_id,
            'status': self.status,
            'stop_reason': self.stop_reason,
            'selection': self.selection,
            'selection_fingerprint': self.selection_fingerprint,
            'position': {
                'page_index': self.page_index,
                'cursor': self.cursor,
                'current_audit_id': self.current_audit_id,
            },
            'progress': {
                'pages_visited': self.pages_visited,
                'entries_seen': self.entries_seen,
                'delivered_media': list(self.delivered_media),
                'delivered_sha256': list(self.delivered_sha),
            },
            'evidence': {
                'list_page_refs': list(self.page_refs),
                'item_records': list(self.item_records),
            },
            'gaps': list(self.gaps),
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Checkpoint':
        if data.get('version') != CHECKPOINT_VERSION:
            raise ValueError('检查点版本不兼容，无法恢复')
        required = ('run_id', 'selection', 'selection_fingerprint', 'position',
                    'progress')
        if any(k not in data for k in required):
            raise ValueError('检查点结构不完整')
        position, progress = data['position'], data['progress']
        evidence = data.get('evidence', {})
        return cls(
            run_id=data['run_id'],
            selection_fingerprint=data['selection_fingerprint'],
            selection=dict(data['selection']),
            cursor=position.get('cursor'),
            page_index=int(position.get('page_index', 0)),
            current_audit_id=position.get('current_audit_id'),
            delivered_media=list(progress.get('delivered_media', [])),
            delivered_sha=list(progress.get('delivered_sha256', [])),
            gaps=list(data.get('gaps', [])),
            pages_visited=int(progress.get('pages_visited', 0)),
            entries_seen=int(progress.get('entries_seen', 0)),
            page_refs=list(evidence.get('list_page_refs', [])),
            item_records=list(evidence.get('item_records', [])),
            status=data.get('status', STATUS_RUNNING),
            stop_reason=data.get('stop_reason'),
        )

    # -- 续取判定 -----------------------------------------------------------
    def is_delivered(self, media_id: str, sha256: Optional[str] = None) -> bool:
        """已领原图不重复交付：以 media_id 为主，内容摘要兜底。"""
        if media_id in self.delivered_media:
            return True
        return sha256 is not None and sha256 in self.delivered_sha

    def mark_delivered(self, media_id: str, sha256: str) -> None:
        if media_id not in self.delivered_media:
            self.delivered_media.append(media_id)
        if sha256 not in self.delivered_sha:
            self.delivered_sha.append(sha256)
        self.current_audit_id = None
        self.status = STATUS_RUNNING
        self.stop_reason = None

    def mark_gap(self, media_id: str, audit_id: str, reason: str) -> None:
        """无法取到的图片明确留缺口，而不是静默跳过。"""
        if not any(g['media_id'] == media_id for g in self.gaps):
            self.gaps.append({
                'audit_id': audit_id, 'media_id': media_id,
                'reason': reason, 'kind': 'missing_original',
            })
        self.current_audit_id = None

    def mark_stopped(self, reason: str) -> None:
        self.status = STATUS_STOPPED
        self.stop_reason = reason

    def mark_complete(self) -> None:
        self.status = STATUS_COMPLETE
        self.stop_reason = None
        self.current_audit_id = None


def save_atomic(path: str | os.PathLike, checkpoint: Checkpoint) -> None:
    """临时文件 + 原子替换，保证检查点不会写坏。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp',
                               dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(checkpoint.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load(path: str | os.PathLike) -> Checkpoint:
    return Checkpoint.from_dict(
        json.loads(Path(path).read_text(encoding='utf-8')))
