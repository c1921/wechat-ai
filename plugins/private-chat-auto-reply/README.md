# 私聊自动回复

标准 WeChat-AI Docker 镜像默认安装 `private-chat-auto-reply`，初始配置为禁用。启用后，插件仅处理白名单联系人发来的私聊文本。它通过 SSH 请求远端服务生成回复，再由 WeChat-AI 的发送队列和数据库回执完成投递确认。插件按会话和消息 ID 在 `turns.sqlite3` 中去重。

## 启用

在部署目录的 `config/config.yaml` 中，把以下配置合并到现有的 `plugins` 段。填写微信联系人 ID，以及运行回复服务的 SSH 主机和用户。

```yaml
plugins:
  private-chat-auto-reply:
    enabled: true
    contact_usernames:
      - wxid_example_one
      - wxid_example_two
    ssh_host: reply.example.org
    ssh_user: reply-bridge
    ssh_directory: /run/private-chat-auto-reply-ssh
    state_directory: /config/private-chat-auto-reply
    conversation_namespace: wechat-private-chat-auto-reply
```

把 SSH 私钥 `id_ed25519` 和主机指纹文件 `known_hosts` 放进部署目录的 `secrets/private-chat-auto-reply/`。在 Compose 覆盖文件中为 `wechat-ai` 添加只读挂载，然后重建容器。

```yaml
services:
  wechat-ai:
    volumes:
      - ./secrets/private-chat-auto-reply:/run/private-chat-auto-reply-ssh:ro
```

SSH 用户登录后应运行一个能从标准输入读单行 JSON、从标准输出写 JSON 的回复服务。请求包含 `op: generate`、`conversation`、`message_id` 和 `text`。成功响应示例为 `{"ok":true,"reply":"回复正文"}`。

### Eros 人设与记忆

插件按会话命名空间、当前微信账号和联系人 ID 派生稳定的 `conversation`，并在同一会话内顺序生成回复。对于 Eros，桥接使用相同的 `genome_id` 人设模板，将每个 `conversation` 映射为独立的 Eros 用户身份。Eros 按用户存画像记忆，按角色实例存关系记忆，聊天历史则跟随各自的会话。[Eros 记忆分层](https://github.com/etherfunlab/eros-engine/blob/main/docs/memory-layers.md)

已有 Eros SSH 桥接部署可使用 [桥接脚本](eros/bridge.py)。它沿用现有 `ErosGenerator`、systemd 凭据和 SSH 转发入口。在桥接配置填入 `allowed_conversations`，让它与微信插件白名单对应。每项是插件发送的 64 位小写十六进制会话标识。用实际登录的微信账号、联系人 ID 和插件中的会话命名空间计算标识。

```python
from hashlib import sha256

def conversation_id(namespace, wechat_account, contact_username):
    return sha256(f"{namespace}\0{wechat_account}\0{contact_username}".encode()).hexdigest()
```

插件在没有 `contact_usernames` 时沿用旧的 `contact_username`。Eros 桥接在没有 `allowed_conversations` 时沿用旧的 `allowed_conversation`。使用新列表时，把仍需保留的旧条目列入其中。

### 一次性邀请码

管理员在后台进入「私聊自动回复」，生成并复制邀请码。请让对方在微信私聊中单独发送它。邀请码 30 分钟内有效，第一位符合条件的联系人用掉它。加入后会排队发送固定提示，下一条普通私聊才进入 Eros。窗口不可发送时，后台仍保留加入记录，并显示确认消息的状态。

后台可以撤销未用的码或移除通过码加入的人。移除会停止后续自动回复，联系人再次用新码加入时可接续原有 Eros 记忆。手工名单仍由 YAML 管理。

部署时，把 `eros/bridge.py` 和 `wechat_invite_admission.py` 安装到 Eros 桥接脚本所在目录。生成一个独立的 32 字节随机密钥，用小写十六进制文本保存；将同一密钥通过既有只读 SSH 目录挂载到 WeChat-AI，文件名为 `invite_admission_hmac`。VPS 通过 systemd 的 `LoadCredential=invite_admission_hmac:/etc/aimee-eros/invite_admission_hmac` 加载同一密钥。在 WSL 的插件设置中启用 `invitations_enabled: true`，重启 Bot 使其加载配置。签发之前会验证两端密钥匹配。

邀请码和加入记录保存在插件现有的 `turns.sqlite3`。发到微信里的邀请码仍会出现在微信聊天记录中。插件保存摘要，且 Eros 只接收后续普通私聊及按次签发的请求凭证。

## 已有部署迁移

将旧的 `plugins.aimee` 配置移到 `plugins.private-chat-auto-reply`，沿用原来的联系人 ID、主机和已定义的状态目录。给新配置填入现有 SSH 用户及密钥挂载路径。旧部署的默认值如下。

```yaml
ssh_user: aimee-bridge
ssh_directory: /run/wechat-aimee-ssh
state_directory: /config/aimee
conversation_namespace: wechat-aimee
```

沿用状态目录和会话命名空间可保留旧的去重记录及远端会话标识。已有 Compose 覆盖文件若使用旧派生镜像的构建配置，将它改为使用本仓库的标准镜像构建，同时保留原来的密钥挂载。替换镜像与配置后，管理面板会显示 `private-chat-auto-reply`。

## 安装选项与测试

构建不含此插件的镜像可执行以下命令。

```bash
docker compose build --build-arg INSTALL_PRIVATE_CHAT_AUTO_REPLY=false wechat-ai
```

从源码开发时，在已安装 WeChat-AI 的 Python 环境中安装插件，再运行测试。

```bash
python -m pip install --no-deps ./plugins/private-chat-auto-reply
python -m unittest discover -s plugins/private-chat-auto-reply/tests
```
