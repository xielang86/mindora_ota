# OTA Server

入口是 `ota_server.py`，部署后由 systemd 管理 Gunicorn，监听 `0.0.0.0:4242`。
脚本会创建 `.venv` 并安装 `requirements.txt` 中的 Flask 和 Gunicorn，无需单独安装 Gunicorn。
Gunicorn 导入 `ota_server:app` 提供服务。
支持使用 apt-get（Ubuntu/Debian）、dnf 或 yum 的 Linux 系统，要求 systemd 正在运行。
部署需要 root 权限，以及可访问系统软件源和 Python 包源的网络。

## 新机器部署

将整个项目复制到服务器的 `/root/server/ota_server`，然后执行：

```bash
cd /root/server/ota_server
sudo bash deploy.sh --with-data
```

`--with-data` 将 `ota/` 复制到 `/root/server/ota_server/ota/`，覆盖同名文件，保留其他已有文件。
从其他目录部署时，符号链接会展开为真实文件；在安装目录内执行时保留已有链接。请确保 `ota/resources` 指向的目录存在且可读。
复制包含版本元数据及其引用的所有固件、资源文件，以免客户端下载失败。
更新已有机器时，应在无下载流量的维护窗口复制数据，避免元数据与固件不一致。

如需单独上传 OTA 数据，可不带参数部署：

```bash
sudo bash deploy.sh
# 将完整 OTA 数据放入 /root/server/ota_server/ota/ 后赋予服务读取权限：
sudo chown -R root:root /root/server/ota_server/ota
sudo chmod -R u+rwX,g+rX,o-rwx /root/server/ota_server/ota
```

不带参数会保留已有 OTA 数据；没有 `version.json` 时，健康检查正常，但 `/ota/check` 返回 503。
由于项目位于 `/root` 下，服务使用 `root` 用户运行；systemd 将系统和 home 目录设为只读。应用位于 `/root/server/ota_server`，
虚拟环境位于 `/root/server/ota_server/.venv`，单元文件位于 `/etc/systemd/system/ota-server.service`。
部署脚本可重复运行，成功后自动启动服务并设置开机启动。

## 更新已安装的服务代码

从发布机执行，可传 SSH 配置别名。脚本按目标机的 `WorkingDirectory` 定位安装目录，
可用第二个参数核对目录。

```bash
./update_server.sh ota-backup
./update_server.sh ota-primary /root/server/ota_server
```

目标机需已用 `deploy.sh` 安装并配置为运行 `ota_server:app`，SSH 用户需为 root 或可使用非交互 sudo。
脚本通过 rsync 上传 `ota_server.py` 和 `ota_reports.py`，使用现有虚拟环境检查能否导入，
替换文件后重启 `ota-server.service` 并检查 `/ota/check`。启动或检查失败时会恢复上一份代码并尝试重启。
多台机器逐台执行。自定义监听端口时，可在发布机设置 `OTA_UPDATE_PORT`。
完整安装、依赖或 systemd 配置更新继续使用 `deploy.sh`，设备代码包和资源使用各自的发布脚本。

## 管理与验证

```bash
sudo systemctl start ota-server
sudo systemctl stop ota-server
sudo systemctl restart ota-server
sudo systemctl status ota-server --no-pager
sudo journalctl -u ota-server -f
curl -f http://127.0.0.1:4242/healthz
curl -f -X POST http://127.0.0.1:4242/ota/check
```

外部设备访问需要在机器防火墙、安全组中允许 TCP 4242，并将客户端 OTA 地址配置为该机器地址。
如需修改端口或 Gunicorn 参数，编辑单元文件中的 `ExecStart`，随后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart ota-server
```

## 发布资源（`publish_resources.py`）

在发布机的 OTA 仓库根目录执行。发布机和目标机需要 Python 3.11+ 和 rsync。发布账号需能通过
SSH 登录并写入目标 OTA 根目录。目标机需已有可访问的 OTA 服务，普通资源发布还需要已有代码发布元数据 `ota/version.json`。

### 1. 准备资源清单

`--source` 指向包含 `manifest.json` 的资源根目录。清单中的 `version` 是资源版本，
脚本会用它命名服务端的资源快照。如果已有确认过的清单，可以直接使用。资源有变动时，
在只包含正式素材的目录中生成新清单，并递增资源版本。

```bash
python3 ota/generate.py /path/to/resources 1.3.5
# 已预置签名密钥的设备需要签名清单：
# python3 ota/generate.py /path/to/resources 1.3.5 --sign-key-file /path/to/ota_sign.key
```

生成器会递归扫描源目录。备份和历史清单应放在源目录之外。向多台镜像发布时使用同一份清单。
发布过的资源版本要求清单字节一致，重试时沿用原清单。

### 2. 配置目标服务器

`--config` 可以复用代码发布的 JSON。单台服务器配置如下。

```json
{
  "ssh_host": "ota-primary",
  "remote_root": "/root/server/ota_server",
  "public_base_url": "https://ota.example.com/ota"
}
```

| 字段 | 用途 |
| --- | --- |
| `ssh_host` | SSH 主机或配置别名，也接受 `user@host`。填写 `localhost` 时在当前机器发布。 |
| `remote_root` | 目标机的 OTA 安装根目录，与该机器的 `INSTALL_DIR` 一致。 |
| `public_base_url` | 以 `/ota` 结尾的 HTTP(S) 地址，目标机自身也必须能访问它以完成发布校验。 |

要用同一份 `online.json` 发布到多台机器，在其中添加 `servers` 数组，把所有目标依次列入，
例如先备用机，再主机。

```json
{
  "servers": [
    {
      "ssh_host": "ota-backup",
      "remote_root": "/srv/ota",
      "public_base_url": "https://ota-backup.example.com/ota"
    },
    {
      "ssh_host": "ota-primary",
      "remote_root": "/root/server/ota_server",
      "public_base_url": "https://ota-primary.example.com/ota"
    }
  ]
}
```

如果代码发布也使用这份 JSON，保留原有顶层字段。资源脚本在存在 `servers` 时按数组发布，
代码发布脚本仍读取原有顶层字段。

### 3. 发布并检查

例如，使用相邻 `../resources` 中已确认的清单，发布到 `online.json` 指定的目标。

```bash
python3 publish_resources.py \
  --source ../resources \
  --config ../mindora/deploy/online.json
```

脚本只同步清单列出的文件。首次可在服务端从旧 `ota/resources/` 复制未变文件，后续从上一版通过
硬链接复用未变文件。暂存目录和发布目录需在同一文件系统。

脚本核对文件大小和哈希，发布到 `ota/resource-releases/<资源版本>/`，再验证公开清单，
使用与代码发布共用的 `.publish.lock` 原子记录最新资源。线上代码没有绑定资源版本时，
这些最新资源会成为对外下载地址。线上代码通过 `resources_version` 绑定了特定版本时，继续使用绑定资源，
该资源目录即使超出保留数量也会保留。
成功输出包含 `resources_url`、`resources_manifest_url` 和 `resources_manifest_sha256`，
并已通过公开 `/ota/check` 校验。若提示 `Resource metadata committed`，说明指针已切换，
可先查询目标的 `/ota/check` 确认状态。
多台机器按配置顺序逐台发布并独立切换。一台失败时脚本继续处理后续目标，最后列出失败目标并返回非零状态。

公开检查通过后，默认保留最近 3 个资源快照。发布较频繁时可加 `--keep 5` 等参数延长旧版本的下载窗口。
历史固定路径 `ota/resources/` 继续供已拿到旧 URL 的设备访问。资源发布无需重启 OTA 服务。
发布单独的资源版本时，客户端的升级触发条件仍由设备上的代码版本逻辑决定。

### 为新代码预传绑定资源

代码发布包的 `version.json` 可设置 `"resources_version": "1.3.4"`。先在每个代码发布目标准备这份资源。
使用 `--stage-only` 时，脚本上传并校验快照，现有公开资源地址和最新资源记录继续沿用。

```bash
python3 publish_resources.py \
  --source ../resources \
  --config ../mindora/deploy/online.json \
  --stage-only
```

后续运行代码发布脚本，它会验证绑定清单，把代码版本和资源地址一起写入 `ota/version.json`。
没有设置绑定的代码包继续使用目标机的最新资源。

## OTA 设备日志

客户端通过 `X-OTA-SN`、`X-OTA-MAC` 和 `X-OTA-Version` 请求头发送设备序列号、网卡 MAC 和运行版本。
Gunicorn 的 `error.log` 收录 `OTA` 前缀的单行 JSON，事件 `ota_check` 对应版本检查，
`ota_download` 对应安装包、资源和资源清单请求，包含 HTTP 状态、路径及请求的 Range。
旧客户端可以继续下载，日志会使用它已有的检查字段，未提供的身份字段留空。
下载事件表示服务处理过该请求，设备健康和安装结果通过独立上报接口记录。

反向代理应将 `/ota/` 的检查与下载请求转发给 Gunicorn，并保留上述请求头。
若由 Nginx 直接返回静态文件或缓存命中，需从代理侧记录设备请求头才能覆盖这些下载。

```bash
sudo rg -F '"serial_number": "DEVICE-SN"' /var/log/ota-server/error.log
sudo tail -F /var/log/ota-server/error.log
```

## Gunicorn 日志落盘与自动清理

部署默认路径已统一为 `/root/server/ota_server`。部署会安装独立的日志轮转 timer，
不依赖 Gunicorn worker 执行清理。配置示例（root 执行）：

```bash
INSTALL_DIR=/root/server/ota_server \
LOG_DIR=/var/log/ota-server \
LOG_RETENTION=14 \
LOG_MAX_SIZE=50M bash deploy.sh
```

参数在部署时写入 systemd 和 logrotate 配置；再次部署时应传入相同的自定义参数，
省略参数将恢复默认值。不带 `--with-data` 不会从源目录覆盖 OTA 数据。

- `access.log`：请求、状态码、返回字节数、请求耗时、连接 IP 和 Nginx 提供的 IP。
- `error.log`：Gunicorn 启停、异常及 OTA 设备事件。
- 默认每天轮转；独立 timer 每小时检查，超过 50 MB 时也会轮转。
- 每个文件保留 14 份归档，超过后自动删除；不是严格保留 14 天。
- 历史日志压缩，最近一次归档延迟压缩，避免进程切换文件时影响写入。
- 轮转采用重命名并向 Gunicorn master 发送 USR1，重新打开日志，不使用 copytruncate。
- 50 MB 是检查时的轮转阈值，不是磁盘硬上限；高流量时文件可在两次检查间超过阈值。

```bash
sudo tail -F /var/log/ota-server/access.log /var/log/ota-server/error.log
sudo systemctl status ota-server-logrotate.timer
sudo journalctl -u ota-server-logrotate.service -n 30 --no-pager
# 只检查配置，不执行轮转：
sudo logrotate --debug --state /var/lib/ota-server/logrotate.status /etc/ota-server/logrotate.conf
# 手动执行一次轮转（用于验证）：
sudo logrotate --force --state /var/lib/ota-server/logrotate.status /etc/ota-server/logrotate.conf
```

日志目录权限为 0750，新日志文件权限为 0640。systemd 只为配置的日志目录开放写权限。
若后续修改目录，重新部署以同时更新写权限、Gunicorn 配置和轮转路径，不要只改环境变量。
不要将日志放入公开下载的 `ota/` 目录。Nginx 自身日志仍由 Nginx 的轮转配置管理。
应用运行日志落盘后，`journalctl -u ota-server` 主要用于服务生命周期和启动失败诊断。
`client` 字段来自请求头，仅用于诊断；Nginx 需覆盖 X-Real-IP，且公网应禁止直连 4242。

仅修改 `server.py` 的服务不需要替换应用代码；本次日志功能由 `gunicorn.conf.py`、service 和部署脚本提供。

## 更新上报与新版客户端

新增独立的 `POST /ota/report`、`POST /ota/health`、`POST /ota/logs`，不更改旧下载协议。
部署脚本保留已有 `/etc/default/ota-server`，首次生成时 token 为空；配置 token 后重启才启用上报。
SQLite 数据存放在 `/var/lib/ota-server`，由 systemd StateDirectory 提供写权限。
设备端稳定版的状态锁、多源选择及原启动方式部署步骤见 [client_update/README.md](client_update/README.md)。
原 `ota_client/` 是旧示例，实际改动针对相邻 mindora 项目；不要混用两套接口。

当前客户端稳定版不启用外置健康确认/自动回滚，不需要安装 OTA supervisor；详见 client_update/README.md。

## 当前发布迭代：仅多源与防重复更新

以 mindora 分支为基线的实际补丁位于 client_minimal/，已同步到相邻 mindora 项目。
不使用前面 client_update/ 的健康确认/启动守护方案。保持原四个进程及启动方式，无新增安装依赖。
设备配置及行为说明见相邻项目 docs/ota-multisource.md；补丁文件位于 client_minimal/mindora/。
