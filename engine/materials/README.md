# 资料与创作服务

资料、引用、原消息记录、截图校对、图片版本以及 Office 成品保存在 `data/materials/`。这是持久存储，不能套用 `data/attachments` 或 `data/artifacts` 的临时清理规则。数据库与 `blobs/` 必须一起备份；对外投递只使用导出到附件目录的副本。

## 部署依赖

在项目实际运行的 Python 环境安装独立依赖：

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-materials.txt
```

PDF 的解析、生成和预览使用 pypdf、reportlab、PDFium。DOCX、XLSX、PPTX 使用 python-docx、openpyxl、python-pptx；预览及工作簿公式重算需要 LibreOffice。应从 LibreOffice 官方分发取得与部署系统匹配的版本并核验下载校验和。可使用独立目录安装，不需要注册为用户默认 Office。

在启动 Supervisor 的环境中设置：

```powershell
$env:CLONOTH_MATERIALS_SOFFICE = 'C:/Program Files/LibreOffice/program/soffice.com'
$env:CLONOTH_MATERIALS_PYTHON = 'E:/project/.venv/Scripts/python.exe'
```

Linux 示例：

```sh
export CLONOTH_MATERIALS_SOFFICE=/usr/bin/soffice
export CLONOTH_MATERIALS_PYTHON=/srv/clonoth/.venv/bin/python
```

`CLONOTH_MATERIALS_PYTHON` 可省略，依次检测工作区 `.venv` 和当前 Python。`CLONOTH_MATERIALS_SOFFICE` 可省略并从 PATH 查找。上述路径只是部署示例，不依赖开发机或 Codex 缓存目录。中文 Office 内容需要安装可用中文字体；Windows 验收使用微软雅黑。Linux renderer 应提供 Noto CJK 等中文字体，并人工复核合成中文预览。PDF 可用 `CLONOTH_MATERIALS_FONT` 指定兼容的 TrueType 字体。

通过设置页“资料与创作”或带凭证的 `GET /v1/materials/capabilities` 检查 Python 库和 Office 渲染器。缺失依赖、渲染失败、空预览、页数超限或公式未计算时，任务明确失败，不能确认或导出没有有效预览的成品。

## 处理隔离与限制

处理在独立 Python 子进程执行，不继承 provider 密钥等任意环境变量。只传基础系统路径和 `CLONOTH_MATERIALS_*` 配置。Windows 使用 Job Object，限制单进程约 1 GiB、作业约 2 GiB，并在 worker 结束时终止所属子进程。POSIX 限制地址空间和 CPU 时间。单次处理有 180 秒超时，Office 转换有 120 秒超时；并行处理槽为 2。

Office 转换只接收服务根据受约束规格生成的文件，不直接执行上传文件。每次转换使用短临时目录和独立 Office 用户配置，禁用宏执行及链接更新；短目录也避免 Windows Office profile 的深路径限制。生产部署仍建议把 renderer 放在专用低权限账户或限制网络的容器中。

输入文件最多 50 MB。文本解析最多 200 页、约 200 万字符；Office 压缩包限制条目数、展开大小和压缩比，拒绝宏、嵌入对象和活动外部资源。导出预览最多 50 页，生成表格最多 50000 个单元格，幻灯片最多 30 页。XLSX 公式只接受明确公式对象和支持的函数，不把普通用户文本当成公式。

## 引用与记录语义

PDF 引用使用文件实际页序。Word 使用正文段落或表格单元格定位，不假造原 Word 排版页码。每个文本引用绑定源文件哈希和提取位置；直接引文必须与保存的原文匹配。扫描 PDF 没有可提取文字时返回具体的 OCR 提示，不把空提取当作“原文件没有内容”。截图结构化由实际可见图片的视觉节点提出草稿，模糊项保留待核对状态，用户校对形成独立修订。

原消息 journal 默认关闭，管理员可按会话开启，只记录开启后实际接收/确认发送的消息。模型不能伪造原消息记录。重复上报不能覆盖原话，只能在同一发送者的真实消息上补充已授权资料引用。检索限制会话、机器人实例与保留期限；压缩后的 ConversationStore 不参与伪造原消息。

## 版本与投递

每个图片或成品版本的内容不可变。恢复旧版生成新的版本记录并复用旧字节，不再次调用模型。编辑使用基准版本和期望当前版本检查并发变化。真实生图调用在开始前登记一次性操作，结果绑定原任务、工具和输入；即使异步工具完成时父任务已经结束，也只能完成这一个已登记操作。任务取消或会话清空后拒绝归档，不能通过放开所有终态任务权限绕过校验。

成品状态经过生成、真实渲染、预览就绪、用户确认。确认绑定具体版本与文件哈希，模型任务不能代替用户确认。Web 逐页读取带 Authorization 的预览 blob，不在外部图片 URL 携带管理员 token。用户批准后才能下载或请求向当前会话发送。发送使用已有持久出站机制；API 返回“已提交发送”不等同于平台已确认收到。

## 重启和停止

启动时，遗留的 `queued`/`running` 本地生成任务变成 `interrupted`，页面和 QQ 支持显式重试。若成品与预览已提交但旧任务状态未更新，验证文件完整性后恢复为 `preview_ready`。重启不会自动批准、自动发送或重新调用付费生图服务。

`state.materials_runtime.close()` 拒绝新任务、标记未完成任务中断、停止所属 worker，再等待所有后台任务及受保护的 `to_thread` 调用结束，最后关闭 SQLite。调用方必须等待该异步关闭方法；不能在解析线程仍使用数据库时直接关闭连接。

## 本地验收

```powershell
.venv/Scripts/python.exe -m pytest tests/test_materials.py -q
```

设置真实 LibreOffice 路径后，测试包含合成 DOCX、XLSX、PPTX 转 PDF/PNG，XLSX 公式重算、预览前导出拒绝、确定版本批准与重新打开验证。未配置 Office 时对应真实渲染测试明确跳过，其余授权、引用、校对、版本、终态异步归档和重启/关闭测试照常执行。前端交互测试为 `src/features/materials/MaterialsPage.test.tsx`。上线前仍应人工逐页复核目标平台的中文字体与实际预览布局。
