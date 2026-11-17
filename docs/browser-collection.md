# 可恢复浏览器采集设计

在既有单响应流程（`source.py`/`extract.py`/`ledger.py`）之上，新增按门户实际
选择状态领取巡检条目与原图的可恢复采集。全部新代码仅依赖标准库，不接触真实
企业系统；真实浏览器通过实现同一驱动协议接入，编排、检查点、收据无需改动。

## 模块划分

| 模块 | 职责 |
| --- | --- |
| `portal_driver.py` | 浏览器驱动协议 `BrowserPort`、领域模型、异常、本地内存门户 `SimulatedPortal` |
| `checkpoint.py` | 原子持久化检查点：续取位置、去重集合、缺口、脱敏证据链 |
| `collector.py` | 采集编排：筛选确认、条件等待、翻页、身份绑定、过期停点与续取 |
| `portal_ledger.py` | 收据组装与独立核验；凭据头统一脱敏 |
| `portal_service.py` | 本地演练 CLI（合成数据），演示完整跑、过期停点、重新登录续取 |

## 门户交互语义（`BrowserPort`）

所有读取都以**门户当前实际状态**为准，编排层不假设点击必然生效：

- `read_selections` / `apply_selections`：级联选择 region→store→day，
  以门户回显的实际选择为准；筛选变化使旧翻页句柄作废。
- `read_first_page` / `read_next_page`：不透明游标（不是全局页码）。游标内嵌
  筛选指纹，驱动在指纹不匹配时抛 `SelectionMismatch`，杜绝筛选变化后页码被
  按新条件重新解释。
- `open_detail` + `wait_until(CONDITION_DETAIL_READY)` + `read_detail`：
  弹层延迟加载，**只靠条件轮询判定就绪**；预算内不满足抛 `WaitTimeout`，
  不使用固定等待时间。
- `fetch_original`：领取前再次确认弹层仍展示该条目（audit_id + media_id），
  身份漂移即拒绝，防止把照片挂到相邻门店。
- 任何方法在会话失效时抛 `SessionExpired`；驱动不提供登录、不保存凭据。

## 可恢复性

检查点（`checkpoint.json`，临时文件 + `os.replace` 原子写入）记录：

- 筛选字段与指纹、当前 `page_index`、下一页 `cursor`、`current_audit_id`；
- 已交付 `media_id` 与原图 `sha256`（双重去重）；
- 明确缺口（含原因）；
- 跨续取累积的脱敏响应引用与条目证据，保证最终收据覆盖整个 run。

会话过期时：编排捕获 `SessionExpired`，把状态置为 `stopped_session_expired`
并落盘后抛 `CollectionAborted`，停在"页 + 句柄 + 条目"的可解释位置。
重新登录后续取：

1. 校验检查点版本与筛选指纹，筛选变化直接拒绝复用旧位置；
2. 重新应用筛选并以门户回显确认；
3. 凭旧游标继续（驱动再做一次指纹校验）；
4. 已交付 media 跳过；取不到的图记 `missing_original` 缺口，不静默跳过。

## 收据与核验

收据 schema `portal-collection-receipt/v1`，每条交付包含
列表条目 → 详情响应引用 → 原图响应引用（url/status/脱敏头/sha256/字节数），
并附 `identity_binding` 断言。`verify_receipt` 独立检查：

- 原图 sha256 与 media_id 均无重复交付；
- 所有身份绑定成立；
- 凭据类响应头（Authorization、Cookie、Set-Cookie 等）一律为 `***REDACTED***`；
- 存在缺口时 `integrity.complete` 为 false，收据不会自称完整。

## 真实 Playwright 适配器映射

实现 `BrowserPort` 时的对应关系（凭据由浏览器会话持有，代码不读取、不落盘）：

| 协议方法 | Playwright 要点 |
| --- | --- |
| `apply_selections` | 依次操作三个控件，`expect`/回显选择器读取实际选中值 |
| `read_first_page` / `read_next_page` | 监听列表 XHR 响应，用响应体构造 `source.Response`；游标从响应/下一页按钮取得 |
| `wait_until` | `page.wait_for_function(...)` 或等待选择器出现且加载态消失，带 timeout，禁止 `time.sleep` 当就绪判据 |
| `read_detail` | 读弹层内 audit_id/media_id，与请求条目比对，不符抛 `SelectionMismatch` |
| `fetch_original` | 在弹层上下文中请求原图 URL，响应封装为 `source.Response`；离开弹层即拒绝 |
| 会话过期 | 识别登录跳转/401/403，统一转成 `SessionExpired` |
| 权限边界 | 只访问门户在当前授权下列出的区域/门店；未授权选择抛 `SelectionMismatch`，不尝试绕过 |

## 安全约束

- 不实现登录自动化、不存储账号口令；会话由运行时在浏览器内建立。
- 日志、检查点、收据只保留响应摘要，凭据头在 `portal_ledger` 统一脱敏，
  并有测试断言凭据值不出现于任何落盘文件。
- `.gitignore` 已忽略 `.env`、`*.key`、虚拟环境等。
