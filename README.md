# Codex

开发流程见[插件开发指南](https://github.com/wontonapp/wonton-docs/blob/main/develop.md)。

Codex 是住在云吞里的 Agent：OpenAI 的 Codex（经 Zed 的 codex-acp 适配器说 ACP）用核心分配的 Matrix 身份和主人聊天，
经 Wonton MCP 总线调用工具。模型网关接好时默认走网关，不用账号、也不用填 key；没接网关时 Agent 照常启动，在聊天里发
`/wonton login`，用设备码登录 ChatGPT 订阅或贴一个自己的 OpenAI API key（登录态存在数据目录，容器重建后还在；
网关接入或卸掉后重启容器即切换）。

## 构建镜像

```bash
docker build -t ghcr.io/geraldpeng6/wonton-plugin-codex:0.1.3 -f images/wonton-plugin-codex/Dockerfile images/wonton-plugin-codex
```

构建出来的是本地 Agent 镜像，`docker image inspect -f '{{.Id}}'` 就是要写回 compose 的摘要。
桥镜像由主仓库维护，`scripts/publish.sh` 只构建本仓库 `images/` 下的 Agent 镜像。

## 聊天里的话题

主人可以在聊天里另开话题并行派活：每条消息按所属话题进各自的会话，做完一轮在主对话里留一行不提示的提示，两边互相能看到
对方的动向；两个话题要改同一个文件时，确认卡片默认等对方做完再改。

话题和主对话一样用 `read-only`：改文件前逐次确认，所以同改一个文件时能看到排队卡片（默认等对方做完再改）。
`WONTON_ACP_TOPIC_MODE` 可以换掉话题的模式，取值必须是 codex-acp 报得出的（它只有 `read-only`、`auto`、`full-access`）：

- `read-only`：和默认一样逐次确认；配了它桥就知道这是“改前先问”的模式，不再在话题里提示没有这种模式。
- `auto`：不出确认卡片，但实测在本容器里改不动文件——它的宽沙箱要的运行时检查和退回的 Landlock 不兼容，
  不配为好。
- `workspace-write` 不是 codex-acp 的模式：桥退回 `read-only`，并在这个话题里提示没有“改前先问”的模式。
  （`full-access` 更宽，未实测。）

compose 里设的是 `read-only`。不设时桥会沿用 `WONTON_ACP_MODE`，行为一样，只是每个话题都会提示没有“改前先问”的模式。

## 图标

`icon.svg` 是按插件规范手绘的线条图标（终端提示符），原创，随插件包以 MIT 许可证分发。Codex 和 OpenAI 是 OpenAI
的商标；本插件不是 OpenAI 的官方出品。来源、上游程序和许可证的说明见 [NOTICE](NOTICE)。
