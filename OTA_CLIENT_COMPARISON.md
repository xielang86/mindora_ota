# OTA 客户端检查结果

对比对象：本项目 `ota_client/` 与相邻项目 `../mindora/ota/`，并检查实际调用方 `../mindora/otaUpdate.py`。
这是本地工作区的静态检查结果，不代表设备当前安装的文件完全一致。

| 项目 | ota_server/ota_client | mindora/ota |
|---|---|---|
| 检查更新 | GET 固定 OTA_SERVER，不带设备参数 | POST OTA_INFO_URL，JSON 带 serial_number、current_version |
| 检查地址 | api.mindora316.com/ota/version.json | Config.API_BASE_URL + /ota/check；本地 Config.API_HOST 仍为 api.mindora316.com |
| MAC 上报 | 无 | 无；SerialNo 来自 libsn.so 的 getVendorSN，是厂商序列号，不能当作 MAC |
| IP 上报 | 无；服务器可观察连接 IP | 同左；NAT 环境通常只能观察公网出口 IP |
| 成功回执 | 无网络回执；有本地 pending/rollback 状态函数 | 无网络回执；切换链接后重启 |
| 下载 | Range、重试、可选 SHA256；finished(str) | .part、退避重试、取消、磁盘预检、更多错误分类；downloadFinished(str) |
| 资源更新 | 简单下载和回滚；可选 HMAC 验签 | 路径边界检查、目标盘临时区、跨盘移动兜底、恢复删除项/移除新增项、可选 manifest_hash |
| 元数据验签 | 有 HMAC 实现，密钥存在才验签 | utils.py 没有 HMAC 实现；manifest_hash 实际调用未传入 |
| 代码安装 | 安全解压、磁盘预检、安装前校验、pending 状态和回滚函数 | 直接 tar.extractall；用户数据链接、依赖安装线程；无相同 pending 回滚实现 |

## 实际调用关系和风险

- mindora/otaUpdate.py 的 checkUpdate() 将 SerialNo、local_version 传给 VersionCheckThread；客户端参数存在，但原服务端忽略请求体。
- 资源列表来自 resources_manifest_url，代码与资源清单已分开；不能直接用本项目客户端整目录覆盖 mindora/ota，函数名、参数和信号均不兼容。
- mindora 的 ResourceUpdater 沿用 QThread.finished（无参数）。run() 内部虽然设置 ok/msg，但没有发送自定义结果信号；调用方 resourcesUpdateFinished() 无条件发出 Restarting。失败也可能进入该界面状态，不能拿它当更新成功证明。
- 真正“更新成功”需要新版本启动且健康检查通过后，发送设备身份、运行版本、资源版本和结果。当前两份代码均未实现该网络回执。
- mindora 资源更新器会再次下载 manifest，未传入 manifest_hash；发布期间清单变化可能导致它与最初计算下载列表的清单不一致。

## 服务端日志

server.py 与 ota_server.py 已增加 ota_check JSON 日志，兼容旧客户端，不改变响应协议。
POST /ota/check 记录已有序列号、当前版本和提供版本；MAC 仅在客户端发送 mac 或 mac_address 时记录。
GET /ota/version.json 只有在 query 参数中携带相应字段时才能记录身份。
日志表示检查请求，不表示下载完成或安装成功。下载请求仍可从 Gunicorn/Nginx access log 查看，但没有设备身份，不能可靠关联到某个 MAC。
只对来自 127.0.0.1/::1 的请求采信 X-Real-IP。Nginx 应设置 proxy_set_header X-Real-IP $remote_addr，并限制公网直连 4242。

部署服务代码后执行 systemctl restart ota-server，再用 journalctl -u ota-server -f 查看。
未修改相邻 mindora 项目，也未修改本项目客户端协议。
