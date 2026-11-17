"""巡检门户的浏览器驱动协议与本地模拟实现。

真实环境由 Playwright/Selenium 适配器实现 :class:`BrowserPort`；本模块只依赖
标准库，用 :class:`SimulatedPortal` 复现门户的关键行为，使采集编排可以在不接触
真实企业系统的前提下被测试：

* region/store/day 三级筛选，筛选变化后旧页码失效并被重新解释；
* 详情弹层延迟加载，必须以条件等待判定就绪，不能用固定等待；
* 会话随时过期，过期后任何领取动作都失败；
* 原图只在"当前弹层条目身份"匹配时交付，防止张冠李戴。

驱动不提供登录能力，也不持有持久化凭据：会话由运行时在浏览器内建立，
驱动只感知会话是否仍然有效。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from .source import Response

# 被视为凭据、不得进入日志或仓库的响应头/请求头名称（小写比较）。
SENSITIVE_HEADERS = frozenset({
    'authorization', 'proxy-authorization', 'cookie', 'set-cookie',
    'x-api-key', 'x-auth-token',
})


class PortalError(RuntimeError):
    """门户交互的基础异常。"""


class SessionExpired(PortalError):
    """会话已过期，必须由运行时重新登录后才能续取。"""


class SelectionMismatch(PortalError):
    """门户实际选择状态/条目身份与领取动作声称的不一致。"""


class WaitTimeout(PortalError):
    """条件等待在预算内未满足。"""


class ImageUnavailable(PortalError):
    """授权原图当前无法取得（门户明确缺失或拒绝）。"""


# 条件等待的命名条件。就绪判定由驱动自己完成，编排层不读取驱动内部状态。
CONDITION_DETAIL_READY = 'detail_ready'


@dataclass(frozen=True)
class Selection:
    """region/store/day 三级筛选的完整身份。"""
    region: str
    store: str
    day: str

    def fingerprint(self) -> str:
        """筛选身份指纹；任一字段变化即变化，用于绑定页码解释。"""
        return hashlib.sha256(
            '\x1f'.join((self.region, self.store, self.day)).encode('utf-8')
        ).hexdigest()[:16]


@dataclass(frozen=True)
class ListEntry:
    """列表页中可领取的一条巡检条目（缩略信息，不含原图）。"""
    audit_id: str
    store: str
    day: str
    media_id: str


@dataclass(frozen=True)
class Page:
    """一次列表读取的结果。cursor 为不透明翻页句柄，不是全局页码。"""
    entries: tuple[ListEntry, ...]
    cursor: Optional[str]
    list_response: Response
    selection: Selection


@dataclass(frozen=True)
class Detail:
    """详情弹层加载完成后的条目身份与原图地址。"""
    audit_id: str
    media_id: str
    image_url: str
    detail_response: Response


def _json_response(source_id: str, url: str, payload: dict, status: int = 200) -> Response:
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return Response(
        source_id=source_id,
        url=url,
        status=status,
        headers=(('Content-Type', 'application/json; charset=utf-8'),
                 ('Content-Length', str(len(body)))),
        body=body,
    )


class BrowserPort(ABC):
    """采集编排所依赖的最小浏览器语义。

    所有读取都以"门户当前实际状态"为准，编排层不得假设点击一定生效。
    任何方法在会话失效时都抛出 :class:`SessionExpired`。
    """

    @abstractmethod
    def read_selections(self) -> Selection:
        """读取门户当前实际选中的 region/store/day。"""

    @abstractmethod
    def apply_selections(self, selection: Selection) -> Selection:
        """按级联顺序选择区域→门店→日期，并返回门户回显的实际选择。

        筛选变化会使列表重置，旧翻页句柄作废。
        """

    @abstractmethod
    def read_first_page(self, selection: Selection) -> Page:
        """在给定筛选下读取第一页。句柄与筛选指纹绑定。"""

    @abstractmethod
    def read_next_page(self, cursor: str) -> Page:
        """凭上一页给出的句柄读取下一页；筛选漂移或句柄失效即报错。"""

    @abstractmethod
    def open_detail(self, entry: ListEntry) -> None:
        """打开详情弹层（可能延迟加载，返回时未必就绪）。"""

    @abstractmethod
    def wait_until(self, condition: str, timeout: float,
                   poll_interval: float) -> bool:
        """轮询等待命名条件成立，而不是使用固定等待时间。

        条件由驱动按门户真实 DOM/网络状态判定（如
        :data:`CONDITION_DETAIL_READY`）；在预算内未成立则抛
        :class:`WaitTimeout`，绝不把"等待了固定时长"当成就绪。
        """

    @abstractmethod
    def is_condition_met(self, condition: str) -> bool:
        """读取命名条件当前是否成立（每次轮询调用一次）。"""

    @abstractmethod
    def read_detail(self, entry: ListEntry) -> Detail:
        """读取已就绪的详情弹层；身份不符时拒绝返回。"""

    @abstractmethod
    def fetch_original(self, detail: Detail) -> Response:
        """领取授权原图；仅当弹层仍展示该条目时交付。"""

    @abstractmethod
    def session_alive(self) -> bool:
        """会话是否仍然有效。"""


@dataclass
class _Modal:
    audit_id: Optional[str] = None
    loaded: bool = False
    load_remaining: int = 0


class SimulatedPortal(BrowserPort):
    """用于测试与本地演练的内存门户，行为见模块文档字符串。

    数据集结构::

        {region: {store: {day: [ {'audit_id','media_id','image': bytes,
                                  'image_url'}, ... ] } }}
    """

    def __init__(self, dataset: dict, *, source_id: str = 'portal-sim',
                 page_size: int = 2, detail_load_polls: int = 3,
                 poll_interval: float = 0.0):
        self._dataset = dataset
        self._source_id = source_id
        self._page_size = page_size
        self._detail_load_polls = max(1, detail_load_polls)
        self._poll_interval = poll_interval
        self._alive = True
        self._selection: Optional[Selection] = None
        self._seq = 0
        self._modal = _Modal()
        # 让这些 media 的原图无法取得。
        self.unavailable_media: set[str] = set()
        # 这些 media 只允许成功交付一次，重复领取将报错，用于验证不重复交付。
        self.served_media: set[str] = set()
        # 观测用调用轨迹（不含任何凭据）。
        self.calls: list[str] = []

    # -- 会话管理（测试钩子；真实适配器由运行时在浏览器内重新登录） ----------
    def expire_session(self) -> None:
        self._alive = False

    def relogin(self) -> None:
        self._alive = True
        self._modal = _Modal()

    def _require_alive(self) -> None:
        if not self._alive:
            raise SessionExpired('会话已过期，请重新登录后续取')

    # -- 选择状态 -----------------------------------------------------------
    def read_selections(self) -> Selection:
        self._require_alive()
        self.calls.append('read_selections')
        if self._selection is None:
            raise PortalError('门户尚未建立任何筛选')
        return self._selection

    def apply_selections(self, selection: Selection) -> Selection:
        self._require_alive()
        self.calls.append('apply_selections')
        # 级联校验：区域必须存在，门店必须在该区域下，日期必须有数据键。
        regions = self._dataset.get(selection.region)
        if not isinstance(regions, dict):
            raise SelectionMismatch(f'区域不存在或未授权: {selection.region}')
        stores = regions.get(selection.store)
        if not isinstance(stores, dict):
            raise SelectionMismatch(
                f'门店 {selection.store} 不属于区域 {selection.region}')
        if selection.day not in stores:
            raise SelectionMismatch(
                f'日期 {selection.day} 在门店 {selection.store} 下不可选')
        # 以门户回显为准：筛选变化时弹层关闭、翻页重置。
        self._selection = selection
        self._modal = _Modal()
        return selection

    def _rows(self, selection: Selection) -> list:
        return self._dataset[selection.region][selection.store][selection.day]

    def _make_cursor(self, selection: Selection, page_index: int) -> str:
        raw = f'{selection.fingerprint()}|{page_index}|{self._seq}'
        return base64.urlsafe_b64encode(raw.encode('ascii')).decode('ascii')

    def _parse_cursor(self, cursor: str) -> tuple[Selection, int]:
        try:
            raw = base64.urlsafe_b64decode(cursor.encode('ascii')).decode('ascii')
            fingerprint, page_index, _seq = raw.split('|')
            page_index = int(page_index)
        except Exception as exc:
            raise SelectionMismatch('翻页句柄无法解析') from exc
        if self._selection is None or fingerprint != self._selection.fingerprint():
            # 筛选条件变化后，旧页码必须被拒绝而不是按新筛选重新解释。
            raise SelectionMismatch('翻页句柄不属于当前筛选，页码拒绝重新解释')
        return self._selection, page_index

    def _build_page(self, selection: Selection, page_index: int) -> Page:
        rows = self._rows(selection)
        start = page_index * self._page_size
        slice_rows = rows[start:start + self._page_size]
        entries = tuple(
            ListEntry(audit_id=r['audit_id'], store=selection.store,
                      day=selection.day, media_id=r['media_id'])
            for r in slice_rows
        )
        payload = {
            'selection': {'region': selection.region, 'store': selection.store,
                          'day': selection.day},
            'page_index': page_index,
            'records': [
                {'audit_id': e.audit_id, 'store': e.store, 'day': e.day,
                 'media_id': e.media_id}
                for e in entries
            ],
        }
        resp = _json_response(
            self._source_id,
            f'https://portal.test/api/list?region={selection.region}'
            f'&store={selection.store}&day={selection.day}&page={page_index}',
            payload)
        next_cursor = None
        if start + self._page_size < len(rows):
            self._seq += 1
            next_cursor = self._make_cursor(selection, page_index + 1)
        return Page(entries, next_cursor, resp, selection)

    def read_first_page(self, selection: Selection) -> Page:
        self._require_alive()
        self.calls.append('read_first_page')
        if self._selection != selection:
            raise SelectionMismatch('门户实际筛选与请求筛选不一致，拒绝领取')
        return self._build_page(selection, 0)

    def read_next_page(self, cursor: str) -> Page:
        self._require_alive()
        self.calls.append('read_next_page')
        selection, page_index = self._parse_cursor(cursor)
        return self._build_page(selection, page_index)

    # -- 详情弹层 -----------------------------------------------------------
    def open_detail(self, entry: ListEntry) -> None:
        self._require_alive()
        self.calls.append('open_detail')
        if self._selection is None:
            raise SelectionMismatch('未建立筛选，不能打开详情')
        # 条目必须仍属于当前筛选的列表，防止把相邻门店的条目挂上来。
        if entry.store != self._selection.store or entry.day != self._selection.day:
            raise SelectionMismatch('条目不属于当前筛选，拒绝打开详情')
        if not any(r['audit_id'] == entry.audit_id
                   for r in self._rows(self._selection)):
            raise SelectionMismatch('条目不在当前筛选列表中')
        # 弹层进入"加载中"：返回时不保证就绪，由 wait_until 轮询。
        self._modal = _Modal(audit_id=entry.audit_id, loaded=False,
                             load_remaining=self._detail_load_polls)

    def wait_until(self, condition: str, timeout: float,
                   poll_interval: float) -> bool:
        self._require_alive()
        self.calls.append('wait_until')
        deadline = time.monotonic() + timeout
        while True:
            # 模拟异步加载：每轮询一次，加载倒计时减一。
            if self._modal.audit_id is not None and not self._modal.loaded:
                self._modal.load_remaining -= 1
                if self._modal.load_remaining <= 0:
                    self._modal.loaded = True
            if self.is_condition_met(condition):
                return True
            if time.monotonic() >= deadline:
                raise WaitTimeout(f'条件 {condition} 在预算内未满足')
            if self._poll_interval:
                time.sleep(min(self._poll_interval,
                               max(0.0, deadline - time.monotonic())))

    def is_condition_met(self, condition: str) -> bool:
        if condition == CONDITION_DETAIL_READY:
            return self._modal.loaded and self._modal.audit_id is not None
        raise PortalError(f'未知等待条件: {condition}')

    def read_detail(self, entry: ListEntry) -> Detail:
        self._require_alive()
        self.calls.append('read_detail')
        if self._modal.audit_id != entry.audit_id or not self._modal.loaded:
            raise SelectionMismatch('当前弹层条目身份与请求条目不一致')
        row = next((r for r in self._rows(self._selection)
                    if r['audit_id'] == entry.audit_id), None)
        if row is None or row['media_id'] != entry.media_id:
            raise SelectionMismatch('详情 media_id 与列表条目不一致，拒绝关联')
        payload = {'audit_id': entry.audit_id, 'media_id': entry.media_id,
                   'image_url': row['image_url']}
        resp = _json_response(
            self._source_id,
            f'https://portal.test/api/detail?audit={entry.audit_id}', payload)
        return Detail(entry.audit_id, entry.media_id, row['image_url'], resp)

    def fetch_original(self, detail: Detail) -> Response:
        self._require_alive()
        self.calls.append('fetch_original')
        # 领取前再次确认弹层仍展示该条目——会话续取或界面漂移都会改变它。
        if self._modal.audit_id != detail.audit_id or not self._modal.loaded:
            raise SelectionMismatch('弹层已离开该条目，拒绝把原图挂到其它条目')
        row = next((r for r in self._rows(self._selection)
                    if r['media_id'] == detail.media_id
                    and r['audit_id'] == detail.audit_id), None)
        if row is None:
            raise SelectionMismatch('原图不属于当前筛选下的该条目')
        if detail.media_id in self.unavailable_media:
            raise ImageUnavailable(f'media {detail.media_id} 授权原图不可用')
        if detail.media_id in self.served_media:
            # 编排层应在检查点中跳过已交付条目；走到这里说明去重失效。
            raise PortalError(f'media {detail.media_id} 已交付，禁止重复领取')
        self.served_media.add(detail.media_id)
        body = row['image']
        return Response(
            source_id=self._source_id,
            url=detail.image_url,
            status=200,
            headers=(('Content-Type', 'image/jpeg'),
                     ('Content-Length', str(len(body)))),
            body=body,
        )

    def session_alive(self) -> bool:
        return self._alive
