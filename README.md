# OTA Server

入口是 `server.py`，部署后由 systemd 管理 Gunicorn，监听 `0.0.0.0:4242`。
脚本会创建 `.venv` 并安装 `requirements.txt` 中的 Flask 和 Gunicorn，无需单独安装 Gunicorn。
Gunicorn 导入 `server:app` 提供服务，不执行 `server.py` 中的 `app.run()`。
支持使用 apt-get（Ubuntu/Debian）、dnf 或 yum 的 Linux 系统，要求 systemd 正在运行。
部署需要 root 权限，以及可访问系统软件源和 Python 包源的网络。

## 新机器部署

将整个项目复制到服务器的 `/root/server/ota-server`，然后执行：

```bash
cd /root/server/ota-server
sudo bash deploy.sh --with-data
```

`--with-data` 将 `ota/` 复制到 `/root/server/ota-server/ota/`，覆盖同名文件，保留其他已有文件。
从其他目录部署时，符号链接会展开为真实文件；在安装目录内执行时保留已有链接。请确保 `ota/resources` 指向的目录存在且可读。
复制包含版本元数据及其引用的所有固件、资源文件，以免客户端下载失败。
更新已有机器时，应在无下载流量的维护窗口复制数据，避免元数据与固件不一致。

如需单独上传 OTA 数据，可不带参数部署：

```bash
sudo bash deploy.sh
# 将完整 OTA 数据放入 /root/server/ota-server/ota/ 后赋予服务读取权限：
sudo chown -R root:root /root/server/ota-server/ota
sudo chmod -R u+rwX,g+rX,o-rwx /root/server/ota-server/ota
```

不带参数会保留已有 OTA 数据；没有 `version.json` 时，健康检查正常，但 `/ota/check` 返回 503。
由于项目位于 `/root` 下，服务使用 `root` 用户运行；systemd 将系统和 home 目录设为只读。应用位于 `/root/server/ota-server`，
虚拟环境位于 `/root/server/ota-server/.venv`，单元文件位于 `/etc/systemd/system/ota-server.service`。
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

`ota/version.json` 保存代码版本、代码包信息及资源地址；资源文件列表只保存在
`ota/resources/manifest.json`。资源新增、修改、删除时，递增独立资源版本并重新生成清单：

```bash
cd /root/server/ota-server/ota
python3 generate.py ./resources 1.3.3
# 已预置签名密钥的设备需要签名清单：
# python3 generate.py ./resources 1.3.3 --sign-key-file /path/to/ota_sign.key
```

这里的版本号是资源版本，与代码版本独立。生成脚本只修改资源 manifest，不修改 version.json。
只更新代码包时不需要重新生成 manifest。资源地址保持不变时，资源更新也无需修改 version.json。
先上传完整资源，再发布与之匹配的 manifest；在线覆盖固定路径仍可能影响正在下载的设备，
建议在维护窗口发布，或采用独立版本目录并保留旧资源。

## OTA 检查日志

更新服务代码后重启 `ota-server`，通过 `journalctl -u ota-server -f` 查看 `OTA` JSON 日志。
日志记录检查请求的连接 IP、序列号、当前版本及提供版本；未发送的 MAC 等字段为空。
具体客户端差异与成功回执限制见 [OTA_CLIENT_COMPARISON.md](OTA_CLIENT_COMPARISON.md)。
