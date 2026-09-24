# S5 本地 Agent 接手 Prompt

先读 README_RESEARCH_S5.md、docs/s5/RESEARCH_PLAN.md、reports/s5/AUDIT_REPORT.md。已有S4证据和SHA不修改。这个Prompt不授权任何真实模型、live probe、付费review、benchmark重跑或余额转移。

交付方已经完成本轮全部可本地处理的统计、原始数据核对、代码回归和新论文。不要重复购买数据，也不要为“全绿”把旧30%门放行。

1. 用新的源码目录执行 `python -B scripts/verify_release.py`。不要覆盖旧目录。当前生产代理src/pi未更改，导入的research/s4c部署编排与新research/s5已更改。
2. 唯一需要宿主环境的实验是新编排入口的Mock差异验收：无gate保持full；旧calibration、错误独立gate SHA、不同runtime/scorer在capture/付费preflight之前拒绝。合成测试许可不得调用真实provider。仅操作本轮进程，不动socat或其他容器。
3. 完整S4原档已在交付方复核。只有归档被修改或需要独立重验时才在外部新目录运行：
   `python -B research/s5/run.py census --delivery /absolute/path/to/S4_complete_root --out /absolute/path/to/new_census`
   输入应是含evidence、release_checks、FINAL_SUMMARY的根目录。预期641请求、8,202,832tokens、27/76实际边界；美元unknown。不要把完整包内精简副本当完整原档。
4. 旧 research/s4 校准函数仅用于复现历史数值。新的运行必须走 research/s4c/deploy.py 的S5 gate校验。风险收据从不替代付费授权。不要直接调用旧decide绕过入口。
5. GitHub当前PR仅有独立assurance模块及核心测试/CI；完整当前源码通过S5 ZIP交付。仓库导入以额外的Git bundle和 docs/s5/GITHUB_IMPORT.md为准，先审查文件清单，禁止上传raw evidence、私人proxy配置、密钥、历史账本及worker。不得改变仓库可见性或force push。
6. 下一轮真实研究只写未授权提案：固定未观察来源、评分、候选、cap、完整分母、统计停止规则。缺明确额度则停止，不发送任何请求。

返回本轮新增的报告、命令退出码、source SHA和最小patch即可。源码未改不用再次上传完整仓库；没有新现场缺口也不需要重新上传641条历史响应。
