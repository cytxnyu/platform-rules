# 平台规则 · platform-rules

面向抖音、小红书、B站、微信视频号、TikTok、X、Instagram 的内容规则审查 Skill。每次审查在线核查官方规则，提供具体位置、条款依据、最小修改和发布渠道建议。

## 安装

仓库发布后可通过 Skills CLI 安装：

```bash
npx skills add cytxnyu/platform-rules --skill platform-rules
```

也可下载仓库，将整个目录作为 `platform-rules` 放入所用 Agent 的 skills 目录。入口为 [SKILL.md](SKILL.md)。

## 使用

向支持 Skills 的 Agent 提供素材并说：

> 请用 platform-rules 检查这条视频是否符合抖音、小红书和视频号规则，给出时间位置、官方依据和最小修改。

支持文字、图片、字幕、视频、音频及常见文档。媒体提取脚本不自动完成 OCR、语音转写或完整语义审查，Agent 必须如实报告审阅范围。用户素材默认本地处理。

## 依赖与更新

核心工具需要 Python 3；视频需要 FFmpeg/ffprobe。浏览器后备需要 Node.js、Playwright 与 Edge 或 Chromium。

```bash
python scripts/sync_rules.py sync --platform all --entry-only
python scripts/sync_rules.py sync --platform douyin --max-new 80
python scripts/extract_content.py /path/to/video.mp4 --out /path/to/review
python -m unittest discover -s evals -p "test_*.py" -v
```

如需浏览器后备，可安装 `npm install --no-save playwright`，再按 [更新流程](references/update-workflow.md) 操作。没有 Edge 时安装 Chromium，并传入 `--channel chromium`。

## 公开版边界

仓库保留规则来源配置、同步程序、审查模板和合成测试。规则快照由使用者本地生成，未包含私人视频、真实审查记录及登录凭证。首次运行前没有预下载档案；HTTP 或浏览器失败不代表平台没有规则。

“每次调用更新”不等于后台监控。可发布、可推荐、可投广告和可变现分别判断；本工具不代表平台官方审核，也不承诺过审。
