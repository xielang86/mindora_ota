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

## 资源清单与代码版本分离

`ota/version.json` 保存代码版本、代码包信息及资源地址。每个资源版本在自己的目录下提供
`manifest.json`。资源新增、修改、删除时，在发布机递增独立资源版本并重新生成清单：

```bash
python3 ota/generate.py /path/to/resources 1.3.5
# 已预置签名密钥的设备需要签名清单：
# python3 ota/generate.py /path/to/resources 1.3.5 --sign-key-file /path/to/ota_sign.key
python3 publish_resources.py --source /path/to/resources --config ../mindora/deploy/online.json
```

发布机和目标机需要 Python 3.11+ 和 rsync，SSH 目标复用代码发布配置中的 `ssh_host`、
`remote_root` 和 `public_base_url`。目标机应已有代码发布元数据和可访问的 OTA 服务。
脚本只同步清单列出的文件，验证文件大小和哈希后发布到 `ota/resource-releases/<资源版本>/`。
首版可从旧 `ota/resources/` 在服务端复用未变文件的副本。后续版本从上一版硬链接复用未变文件，
以 rsync 增量传输新内容。暂存目录和发布目录需在同一文件系统。

脚本验证公开资源清单后，与代码发布共用 `.publish.lock` 原子更新 `ota/version.json` 的资源字段。
公开检查成功后默认保留最近三版，可用 `--keep` 增加保留数，为长时间离线或续传的设备延长窗口。
现有旧版固定资源目录继续提供下载，已发布资源版本的文件需保持原样。
发布单独的资源版本时，客户端的升级触发条件仍由设备上的代码版本逻辑决定。

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
