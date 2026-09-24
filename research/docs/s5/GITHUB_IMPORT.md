# GitHub 完整实现导入（不上传私有实验档案）

当前远程 main 只有初始化 README；PR #2 包含独立 assurance 层、75 项核心测试和 CI，不是整个 agent。远程 CI 的首轮失败没有步骤或可用日志，见 issue #3，不能据本地通过强行合并。

本交付另提供 `ReVerPi_S5_Code_Import.bundle`，它包含当前实现代码、包元数据、研究模块、源码测试、文稿与文档，但不包含运行账本、raw wire、gold/私有实验历史或回归依赖的全部数据。全量回归所需材料在完整 S5 ZIP 中，代码导入分支的 CI 只运行不需要这些私有 fixture 的 75 项合同测试。

bundle 与现有 main 共有真实祖先，不会创建不相关历史。它已在第二个干净仓库中验证并取出目标提交。生成 bundle 不等于已经推送。不要将完整私有研究 ZIP 原样上传仓库，不改变 visibility、不强推。

在已授权且工作区干净的本机仓库中：

```bash
# 先用附带SHA文件校验下载的bundle
sha256sum /absolute/path/ReVerPi_S5_Code_Import.bundle

git fetch origin main
git bundle verify /absolute/path/ReVerPi_S5_Code_Import.bundle
git fetch /absolute/path/ReVerPi_S5_Code_Import.bundle \
  refs/heads/research/s5-full-source-import:refs/heads/research/s5-full-source-import
git switch research/s5-full-source-import
git rev-parse HEAD
git merge-base --is-ancestor b2333898e75f317b78ddee57335448b724df741a HEAD
git diff --stat origin/main...HEAD
# 审查 CODE_IMPORT_MANIFEST.json；不需要模型调用
python -B -m pytest research/s5/tests/test_s5_contracts.py research/s5/tests/test_s5_statistics.py research/s5/tests/test_s5_assurance.py -q
# 只有确认文件范围后才推送分支，不自动合并
git push -u origin research/s5-full-source-import
```

预期 HEAD：`c87436bb267e08fb6f9cc6b81bc4479c0d7272c6`。

若 origin/main 已新增其他开发提交，不覆盖、不reset；保留分支并用正常 PR 解决差异。若本机 git 认证不可用，报告阻塞，不寻找或打印密钥。授权只用于用户已指定的私有项目，不将证据上传第三方服务。

完整S5源包包含完整实现和必要测试fixture；此bundle故意更窄。尚需仓库管理员查看Actions运行失败的注解。没有新模型额度，风险许可不等于费用许可。
