# MinerU 提取与缓存

仅在开头询问并得到用户选择后使用本分支；不选择时跳过本文件、CLI 检查与安装。用户指定使用 MinerU 时实际调用，不把本地 PyMuPDF 抽取说成 MinerU。CLI 可单独安装，不需要安装整套 MinerU 技能。先检查 `mineru-open-api version` 和 `extract --help`；模式、限制以实际版本与服务返回为准。

按目的选择模式：用户要求先试工具、小文档文本提取且未配置 token 时，可先用 flash；需要完整图片、表格或公式资产时采用 precision，并核查凭据。不要把 flash 的文本调用成功等同于资产完整。精确模式可通过环境变量 `MINERU_TOKEN` 临时提供凭据（跨平台通用），Windows 上也可用 `python scripts/save_mineru_credential.py` 保存到当前 Windows 用户的凭据管理器（Linux/macOS 没有该助手，请一律用 `MINERU_TOKEN`）。技能脚本会自动读取该凭据，并仅在调用子进程期间设置环境变量。也可用 `mineru-open-api auth` 配置 CLI 凭据。不要把凭据放入技能文件、项目环境文件、Git 或聊天记录；凭据不是翻译模型 Token 用量。

```powershell
python -X utf8 scripts/mineru_extract.py --source paper.pdf --output work/extraction --mode precision
```

免登录模式可供小文档试提取，但不默认声称其输出完整：

```powershell
python -X utf8 scripts/mineru_extract.py --source paper.pdf --output work/extraction --mode flash
```

Windows 通过 npm 安装时，PATH 可能只有 `.ps1/.cmd` 包装脚本。使用 `--cli` 指定已安装的原生 MinerU 可执行文件；不要将某台电脑的绝对路径写进分享包。完整性核对包括跨页方法、图注、反应步骤与公式，图和公式原貌仍以原 PDF 为准。

附带脚本调用远程服务，不支持直接连接本地/私有部署；这类部署需单独按其接口接入，不能声称脚本已支持。源 PDF 内容会提交至 MinerU 服务；对未发表、保密文件先确认外传授权，或使用用户已有的本地/私有部署。公开论文也不把提取结果、下载签名链接或凭据提交到技能仓库。

缓存键包含源文件哈希、CLI 版本、模式、模型及识别选项。命中缓存前核对输出文件哈希；失败不记为可用缓存。证书校验失败不能用关闭 TLS 验证来规避，也不能把失败等待时间标成成功提取耗时。

失败时说明原因并停止此分支，不循环重试或静默换工具。已有本地处理授权时直接继续本地提取，标明实际方式；需要尚未授权的上传或外部处理时才询问。已有有效结果可继续复用。

## 实测记录与完整性边界

验证状态按每篇实际 extraction-report 记录：CLI 版本、模式、成功或失败、耗时及缓存命中。历史证书失败不代表当前服务一直不可用，当前成功也不保证下一篇无遗漏；须对照原 PDF 核对摘要、跨页内容、公式与化学式，不以工具成功代替内容完整性。

官方 CLI 文档：https://github.com/opendatalab/MinerU-Ecosystem/tree/main/cli/mineru-open-api
