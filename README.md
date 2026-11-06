# Codex

开发流程见[插件开发指南](https://github.com/geraldpeng6/wonton-docs/blob/main/develop.md)。

Codex 是住在云吞里的 Agent：OpenAI 的 Codex（经 Zed 的 codex-acp 适配器说 ACP）用核心分配的 Matrix 身份和主人聊天，
经云吞模型网关调用模型，经 Wonton MCP 总线调用工具。模型默认不用账号、也不用填 key；要用 ChatGPT 订阅，在聊天里发
`/wonton login` 用设备码登录官方账号。

## 构建镜像

```bash
docker build -t ghcr.io/geraldpeng6/wonton-plugin-codex:0.1.0 -f images/wonton-plugin-codex/Dockerfile images/wonton-plugin-codex
```

构建出来的是本地 Agent 镜像，`docker image inspect -f '{{.Id}}'` 就是要写回 compose 的摘要。
桥镜像由主仓库维护，`scripts/publish.sh` 只构建本仓库 `images/` 下的 Agent 镜像。

## 图标

`icon.svg` 是按插件规范手绘的线条图标（终端提示符），原创，随插件包以 MIT 许可证分发。Codex 和 OpenAI 是 OpenAI
的商标；本插件不是 OpenAI 的官方出品。来源、上游程序和许可证的说明见 [NOTICE](NOTICE)。
