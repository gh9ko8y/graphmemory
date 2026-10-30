# 隐私与安全说明

本仓库在发布前做过脱敏处理。**不含任何真实用户数据或凭据。**

## 已移除的内容

| 类型 | 处理方式 |
|---|---|
| API 密钥（LLM / Embedding） | 全部改为环境变量，仓库内无任何 key |
| 数据库密码 | 默认值改为公开的开发占位 `graphmemory_dev`；示例用 `${VAR:-default}` 注入 |
| IM 账号（QQ 号、群号） | 全部替换为占位符 `100000001` / `100000002` |
| 真实人名/昵称 | 替换为 `用户A`、`用户B`、`assistant` |
| 服务器 IP / SSH 密码 / 端口 | 未包含 |
| 聊天记录 / 用户档案 / episodes | 未包含（仅 schema） |
| 数据库转储 / 备份 | 未包含（`.gitignore` 已排除 `backups/`、`*.sql.gz`、`*.dump`） |
| 私有路径（个人机器目录） | 改为环境变量或 `/tmp/` 默认值 |
| 私有设计文档引用 | 泛化为通用描述 |

## 提交前检查清单

```bash
# 1) 确认没有 .env
git status --short | grep -E '\.env' && echo "危险：.env 被跟踪"

# 2) 扫密钥/账号
grep -rInE "sk-[A-Za-z0-9]{10,}|[0-9a-f]{32}\.[A-Za-z0-9]{16}" . --exclude-dir=.git

# 3) 扫真实账号（按你自己的列表补）
grep -rInE "你的QQ号1|你的QQ号2" . --exclude-dir=.git

# 4) 扫服务器信息
grep -rInE "你的服务器IP|你的域名" . --exclude-dir=.git
```

## 运行时的安全建议

- 生产环境至少改三处：数据库密码、`API_KEY`、Neo4j/PostgreSQL/Redis 端口仅监听 127.0.0.1
- 不要把 `.env`、`backups/`、`*.log` 加入版本库
- Neo4j/Postgres 不要直接暴露公网；如需远程访问请走 SSH 隧道或内网
- LLM/Embedding 的 key 建议用最小权限、独立计费的子账号

## 数据删除承诺

系统内「删除」采用**墓碑 + 物理删除 + 原文保留**三层策略：

- `memory_forget` / `memory_dedupe` 会写墓碑表并删除 `fact_state` / `memory_state` / `memory_fragments`
- `episodes`（原文账本）默认保留，用于审计；如你的合规场景要求连原文一并删除，请自行扩展删除逻辑
- TTL 到期只先隐藏，宽限 1 天后才物理删除
