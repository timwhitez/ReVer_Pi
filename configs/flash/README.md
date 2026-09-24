# Flash-only 配置

两个真实 Provider 样例均请求 deepseek-flash、effort=low、concurrency=1。
复制已验证的本地 Provider 修改并发即可；不要覆盖历史配置，不要改模型别名。
网关别名不证明实际权重身份：记录响应 model 字段和部署映射，映射变化就另开经批准代次。
max_output_tokens=65536 沿用预留，不声明上游落实硬上限。只选择一个已验收协议开展本轮真实实验；另一个协议用离线合同测试，不为协议交叉重复购买相同实验。

provider.mock.json 是脚本化离线传输，不是任何模型的能力测量。inventory.DEMO.json 仅测抽样工具，不能用于官方结果或模型评估。
review_scope.json 是明确的变更实现范围，依赖代码未全量送给模型；不足上下文必须报告，不允许伪造全树证书。
compression.json 仅为新研究候选起点；18000 字节是记忆预算而非 tokens。不得为追求压缩触发把原生比较的任务、输出、步数或测试预算偷偷缩短。
