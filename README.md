# 门店货架巡检门户的状态化采集

当前程序只接收一份来源 JSON 响应并生成条目收据。source.py 保存响应状态、头和原始字节；extract.py 读取现有 records 条目；ledger.py 绑定来源与响应摘要；service.py 提供单响应入口。项目使用 Python 标准库，不接触真实企业系统。现有单响应流程不证明跨页、跨资源或跨版本采集完整。

来源与字段约定见 docs/source-contract.md。

## 可恢复浏览器采集

新增按门户实际选择状态领取巡检条目与授权原图的可恢复流程（设计见
docs/browser-collection.md）：

- `app/portal_driver.py`：浏览器驱动协议 `BrowserPort` 与内存模拟门户，
  覆盖筛选漂移、详情延迟加载、会话过期、原图身份绑定等行为；
- `app/checkpoint.py`：原子检查点，记录续取位置、已交付去重、缺口与脱敏证据；
- `app/collector.py`：条件等待（无固定 sleep）、翻页游标带筛选指纹、
  过期停在可解释位置、重新登录后先重确认身份再续取；
- `app/portal_ledger.py`：连接门店/日期/条目/图片响应的可核验收据，凭据头统一脱敏。

本地演练（合成数据）：

    python -m app.portal_service out/                 # 完整跑一遍
    python -m app.portal_service out/ --expire-after 2  # 第2张后会话过期
    python -m app.portal_service out/ --resume        # 重新登录后续取

运行当前示例：

    python -m app.service fixtures/sample.json

运行测试：

    python -m unittest discover -s tests -v

检查编译：

    python -m compileall -q app tests
