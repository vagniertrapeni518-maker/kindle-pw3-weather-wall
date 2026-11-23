---
generated: true
---

# Kindle PW3 壁挂天气台历 · 完整复现手册（Runbook）

> 已在 **Kindle Paperwhite 3（第 7 代，固件 `5.16.2.1.1`）** + **Ubuntu 22.04 云服务器** 真机连续多周期无人值守验证通过。

## 1. 准备工作与安全原则

1. **备份个人文件**：将 Kindle 通过 USB 连电脑，复制 `documents/` 等个人文档到电脑留存（注意：USB 备份可恢复个人文件，不能作为系统底层救砖镜像）。
2. **全程零修改只读根分区 `/`**：本方案不启用 `enable-boot`（不执行 `mntroot rw` 写 `/etc/upstart/`），全部台历脚本仅运行于用户分区 `/mnt/us/`；任何时候长按电源键 ~10 秒重启即可安全恢复原厂 Kindle 界面。
3. **防止静默 OTA 升级**：越狱连网前，先把 Kindle 置于飞行模式，确认根目录无 `*.bin` 或 `*.partial` 更新包，再运行 `scripts/prepare_anti_ota.py` 将可用空间临时压至 **50–90 MB**（推荐 70 MB）。

```bash
python3 scripts/prepare_anti_ota.py --drive D:/ --keep-mb 70
```

## 2. WinterBreak2 越狱（免 KUAL / MRPI / USBNetwork）

1. 从 [KindleModding/Winterbreak2](https://github.com/KindleModding/Winterbreak2/releases) 下载 `wb2.zip`，解压后将 `winterbreak2/` 目录放入 Kindle 根目录。
2. 安全弹出 Kindle，关闭飞行模式连上 Wi-Fi。
3. 打开 Kindle 体验版网页浏览器，访问 `https://penguins184.xyz/wb2`，点击 **Jailbreak**，弹出下载 `Winterbreak2.mobi` 确认框时点 **确定**，等待 1–2 分钟自动重启。
4. 重启后书库出现 `JAILBROKEN` 文档即成功（`jb.sh v2.6.0+` 会自动禁用 `/usr/bin/otaupd` 并内置 `sh_integration`，支持在书库直接点击运行 `.sh` 脚本，无需额外安装 KUAL、MRPI 或 SSH 插件）。

## 3. 部署服务端（Ubuntu 22.04 示例）

服务端仅依赖 Python 3 标准库 + Pillow + Noto CJK 中文字体，内存占用约 25–30 MB，与宿主机其他服务完全隔离。

```bash
# 1. 安装依赖（Ubuntu 22.04 若弹出 needrestart 重启服务界面，建议选 <Cancel> 避免影响现有容器/服务）
sudo apt-get update && sudo apt-get install -y python3-pil fonts-noto-cjk

# 2. 放置服务端代码
sudo mkdir -p /opt/kindle-wall
sudo cp server/weather_server.py /opt/kindle-wall/

# 3. 生成环境变量与 32 位随机访问令牌
cp server/kindle-wall.env.example /tmp/kindle-wall.env
TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
sed -i "s/REPLACE_WITH_32_CHAR_RANDOM_TOKEN/$TOKEN/" /tmp/kindle-wall.env
# 按需编辑 /tmp/kindle-wall.env 中的 CITY_TITLE / ADCODE / LATITUDE / LONGITUDE / WEEKLY_SCHEDULE
sudo mv /tmp/kindle-wall.env /etc/kindle-wall.env
sudo chmod 600 /etc/kindle-wall.env
echo "Your WALL_TOKEN: $TOKEN"

# 4. 配置 systemd 服务（默认监听独立端口 8088）
sudo cp server/kindle-wall.service.example /etc/systemd/system/kindle-wall.service
sudo systemctl daemon-reload
sudo systemctl enable --now kindle-wall

# 5. 验证鉴权与出图
curl -s -o /dev/null -w "no-token=%{http_code}\n" http://127.0.0.1:8088/wall.png
source /etc/kindle-wall.env
curl -s -o /tmp/check.png -w "with-token=%{http_code}\n" -H "X-Wall-Token: $WALL_TOKEN" http://127.0.0.1:8088/wall.png
```

随后在云服务器安全组入方向放行 `TCP 8088`（因家庭宽带公网 IP 会动态变化，访问控制由 32 位随机 `X-Wall-Token` 负责，无令牌访问一律返回 `404`）。

## 4. 部署 Kindle 客户端并验收

1. 复制 `kindle/config.example` 为本地 `kindle_config.txt`，填入公网地址与随机令牌。首次测试建议先把 `REFRESH_MINUTES` 设为 `5`（快速验证 2 个周期）。
2. 运行部署脚本（强制 Unix LF 换行，可选同时清理越狱占位文件）：

```bash
python3 scripts/deploy_to_kindle.py --drive D:/ --config kindle_config.txt --clean-jailbreak-tmp
```

3. 安全弹出 Kindle，关闭飞行模式，**等右上角出现 Wi-Fi 图标后再等 5 秒**，点击书库里的 **`03-WeatherWall`**。
4. 放手不碰 Kindle，观察连续 2 个周期：
   - 休眠期间屏幕保持清晰全刷画面，不弹出 Kindle 原厂屏保；
   - 到达整点周期自动唤醒，底部 `本机取数` 时间向后推进。
5. 验收通过后，将 `REFRESH_MINUTES` 改为 `60`、`RETRY_MINUTES` 改为 `15` 重新部署一次即可正式挂墙。

## 5. 实机踩坑速查

| 现象 | 原因 | 解法 |
| --- | --- | --- |
| 休眠后屏幕发白、字迹极浅 | `eips -g` 后不到 1 秒就 `echo mem`，打断了 EPDC 墨水粒子驱动 | 改为 `eips -c; sleep 1; eips -f -g; sleep 3` 再进入休眠 |
| Ubuntu 22.04 报 `PIL.Image has no attribute 'Resampling'` | 系统自带 Pillow 9.0.1，9.1+ 才有 `Image.Resampling` | 使用 `getattr(getattr(Image, "Resampling", Image), "LANCZOS")` |
| 顶部显示 `上次实况 · 发布` | 国内接口返回 `"7 分钟前发布"` 等中文相对时间 | `normalize_report_time()` 自动换算为标准 `YYYY-MM-DD HH:MM` |
| 点启动后停留在缓存图 | 点脚本时 Wi-Fi 尚未连稳就停了 `framework` | 长按电源键 10 秒重启，等右上角出现 Wi-Fi 图标后再点启动 |
| 电脑挂 VPN 访问 `8088` 超时 | 代理节点拦截非标准端口 | 用手机直连家里 Wi-Fi（关 VPN）验证端口是否秒返回 `404` |
