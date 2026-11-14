# Docker Compose部署说明

日期：2026年11月14日。用途：准备后续单服务器部署，**本次没有部署或启动真实监控**。

## 文件与入口

- `compose.yaml`：监控服务、Redis、专用缓存网络和应用数据卷；保留旧单平台`serve`入口。
- `compose.console.yaml`：将应用切换为当前多平台`console`入口，端口仍为8088，数据目录为`/app/data/console`。
- `Dockerfile`与`requirements.txt`：生产镜像安装已锁版本和哈希的依赖，包含Redis客户端8.1.0。不是在容器每次启动时在线pip安装。

建议在服务器使用 `docker compose -f compose.yaml -f compose.console.yaml` 组合。应用镜像需要在准备部署时构建；本仓库尚未承诺有可拉取的1.1发布镜像。

## 首次部署（Linux）

准备Docker Engine及Compose插件。不要同时运行旧服务和新服务连接同一机器人。

```bash
# 在项目根目录，仅首次复制；后续不要覆盖已有.env
[ -f .env ] || cp .env.example .env
# 修改你需要的非敏感查询参数；多平台令牌后续在网页填写

docker compose -f compose.yaml -f compose.console.yaml build watch

# 仅初始化一次，密码自动生成，初始化不启动平台和机器人
docker compose -f compose.yaml -f compose.console.yaml run --rm --no-deps watch console-init --control-dir /app/data/console --username admin

# 在私人终端读取初始账号，不能将密码输出分享到群聊
docker compose -f compose.yaml -f compose.console.yaml run --rm --no-deps --entrypoint cat watch /app/data/console/initial-account.txt

docker compose -f compose.yaml -f compose.console.yaml up -d
docker compose -f compose.yaml -f compose.console.yaml ps
```

注意：命名卷默认由Compose项目名确定。第一次部署后保持项目目录/项目名一致，或固定`-p`参数；不要随意换目录名导致挂载新的空卷，看起来像“数据丢失”。已有服务器的数据库或主密钥不要重新生成。

## 访问与反向代理

应用只发布`127.0.0.1:8088:8088`。服务器上可将HTTPS域名反向代理至`http://127.0.0.1:8088`，并转发正常的Host和代理协议头。若反向代理也是容器，容器内的127.0.0.1不是宿主，需要另外配置共享网络和应用上游地址，不要直接复制宿主代理地址。

Console默认安全Cookie，需要浏览器通过HTTPS访问；HTTP仅适合明确的本机开发场景，不在服务器Compose中默认启用`--insecure-cookie`。不会提供注册界面，使用初始化生成的唯一管理员账号登录。

## Redis配置和隔离

默认服务地址：`redis://redis:6379/0`。Redis只连接Compose专用内部网络，没有宿主端口；不应为方便连接而加`6379:6379`。

- 使用官方Redis 8.10.2镜像，按内容摘要固定。
- 健康检查：`redis-cli ping`；首次启动应用需Redis健康。
- Redis以镜像内的`redis`用户运行，只读根文件系统，数据目录用临时内存文件系统。
- Redis缓存上限128MB，容器内存上限256MB，淘汰策略`allkeys-lru`。
- 关闭RDB与AOF，不保存缓存到磁盘；重启缓存丢失只造成重新查询，不丢监控和任务状态。
- 此隔离模式下没有默认Redis密码；宿主及Docker管理员仍是可信边界。需要远程Redis时自行使用鉴权和受保护网络，不将无鉴权Redis暴露到公网。

可选应用参数继续使用现有`.env`：

```dotenv
WATCH_QUERY_CACHE_TTL_SECONDS=30
WATCH_QUERY_TASK_REQUEST_BUDGET=5
WATCH_QUERY_TASK_SECONDS_BUDGET=20
WATCH_QUERY_TASK_MAX_REQUESTS=10000
```

正常组合不用设置`WATCH_QUERY_REDIS_URL`。如果已有外部Redis，可以明确设置该变量；显式`WATCH_QUERY_REDIS_URL=`表示应用只用内存缓存。当前Compose仍会启动内置Redis；如果完全不想启动它，应另行使用经过审查的Compose覆盖配置，不能以删数据库卷代替。

修改`.env`后重新创建应用容器以加载配置，不需要重启NewAPI。Redis运行中不可用时应用自动回退内存；`depends_on`只负责启动顺序，不会因为缓存短暂不健康就终止已运行的应用。

## 常用维护命令

```bash
# 服务状态
docker compose -f compose.yaml -f compose.console.yaml ps
# Redis连通性，不需要开放6379端口
docker compose -f compose.yaml -f compose.console.yaml exec redis redis-cli ping
# 查看日志（输出前检查不要分享凭证）
docker compose -f compose.yaml -f compose.console.yaml logs --tail=100 watch redis
# 升级应用：先备份，再构建和重新创建watch，不更改NewAPI
docker compose -f compose.yaml -f compose.console.yaml build watch
docker compose -f compose.yaml -f compose.console.yaml up -d --no-deps watch
# 停止此项目，默认保留watch-data数据库卷
docker compose -f compose.yaml -f compose.console.yaml down
```

不要随意运行`down -v`，这会删除应用命名卷。重要数据是整个`/app/data/console`和其中的`master.key`，不是Redis缓存。数据库运行中备份应采用在线backup；也可停止应用后整体备份卷，包含SQLite的WAL状态。业务库当前v6、控制库v2，旧版本会拒绝更高schema；回滚需恢复备份，不要强改版本号。

## 本次验证范围

- Compose真实渲染测试覆盖默认Redis地址、健康依赖、内网/无宿主端口、资源限制、外部地址/空地址覆盖及Console启动参数。
- 只启动隔离Compose项目中的Redis，没有构建新应用镜像。
- 使用既有Python容器和当前源码做一次性合成数据探测，Redis客户端从生产requirements的哈希条目离线安装到临时目录。
- 已验证Redis健康、容器内DNS、跨缓存实例命中、平台隔离、30秒TTL、128MB缓存上限和关闭磁盘持久化。
- 隔离项目的容器、网络和临时数据卷已删除；没有接触真实NewAPI、企微、生产数据库或本地.env。
- 没有把这些验证描述为完整应用镜像构建或生产HTTPS部署验收。日报/周报、人民币和机器人能力权限功能本轮不修改。
