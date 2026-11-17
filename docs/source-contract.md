# 来源契约

门户本地演练服务含 region、store、day 三级筛选，列表条目 audit_id 在门店内稳定；详情接口返回 media_id 和授权原图地址。页面没有稳定全局页码，筛选变化会重排。真实凭据由运行时注入；fixture JSON 是现有单条索引概览。

已有单响应输入为 fixtures/sample.json，含 source_id、url、status、headers 和 body_base64；原件摘要由 source.Response.sha256 给出。extract.extract 目前只读取 records 列表中的 audit_id，不能把空列表解释为完整采集。ledger.receipt 把单次响应摘要和提取条目关联，尚无跨请求的完成资格或恢复状态。

来源配置与真实凭据由运行时提供，样例为合成数据。
