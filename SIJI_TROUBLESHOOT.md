# 司机签到自用 vs Mox签到自用 对比排查

## 对比结果：结构完全一致

| 检查项 | Mox签到自用 (moxsignin) | 司机签到自用 (sijishe) | 结果 |
|--------|------------------------|------------------------|------|
| `__init__.py` 存在 | ✅ | ✅ | 相同 |
| `requirements.txt` 存在 | ✅ | ✅ | 相同 |
| `package.v2.json` 条目结构 | name/description/labels/version/icon/author/level/history | name/description/labels/version/icon/author/level/history | 相同 |
| 类定义 | `_PluginBase` 子类 | `_PluginBase` 子类 | 相同 |
| 关键属性 | plugin_name/plugin_version/plugin_author/... | plugin_name/plugin_version/plugin_author/... | 相同 |
| 文件编码 | UTF-8 | UTF-8 | 相同 |
| Python 语法检查 | 通过 | 通过 | 相同 |
| GitHub API 文件访问 | 200 OK | 200 OK | 相同 |
| raw.githubusercontent 访问 | 200 OK | 200 OK | 相同 |
| 图标文件 | 200 OK | 200 OK | 相同 |

**结论：仓库文件本身没有任何问题。**

---

## 可能的根因（按概率排序）

### 1. MoviePilot 第三方市场地址格式错误 ⭐⭐⭐⭐⭐

**最可能的原因。**

MoviePilot v2 正确格式：
```
https://github.com/Vivitoto/MoviePilot-Plugins
```

常见错误格式（会导致 404）：
```
https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/package.v2.json
```

**检查路径：** MoviePilot → 设置 → 插件 → 第三方插件市场仓库地址

### 2. MoviePilot 版本太老 ⭐⭐⭐⭐

`package.v2.json` 是 **MoviePilot v2** 专用格式。

如果 MoviePilot 是 **v1 版本**，它只读取 `package.json`，而你的 `package.json` 是空的 `{}`，所以所有插件都装不了。

**检查：** 看系统信息里的版本号，确认是 v2.x.x

### 3. MoviePilot 容器网络不通 ⭐⭐⭐

虽然错误显示 "404"（说明请求到达了 GitHub），但可能是容器内 DNS 或代理配置有问题。

**测试：**
```bash
docker exec <moviepilot容器名> curl -I https://api.github.com/repos/Vivitoto/MoviePilot-Plugins/contents/plugins.v2/sijishe/__init__.py
```

### 4. MoviePilot 缓存了旧的 package.v2.json ⭐⭐

如果之前加载过市场，可能缓存了旧数据。

**解决：** 重启 MoviePilot 容器，或进入插件市场页面手动刷新。

### 5. GitHub API 速率限制（伪装成 404）⭐

没有配置 GitHub Token 时，频繁操作可能触发限制。

**解决：** MoviePilot → 设置 → 高级 → GitHub Token 里填一个 Personal Access Token

---

## 快速诊断步骤

请你按顺序执行，告诉我结果：

**步骤 1：确认市场地址**
进入 MoviePilot → 设置 → 插件 → 第三方插件市场仓库地址
把截图或完整配置发给我

**步骤 2：确认 MoviePilot 版本**
看系统信息里的版本号，发给我

**步骤 3：容器内测试网络（如果方便）**
```bash
docker exec <moviepilot容器名> curl -s https://api.github.com/repos/Vivitoto/MoviePilot-Plugins/contents/plugins.v2/sijishe/ | head -c 200
```

**步骤 4：尝试手动安装**
如果以上都正常，可以手动把文件放进 MoviePilot：
```bash
# 进入 MoviePilot 容器或宿主机映射目录
mkdir -p /path/to/moviepilot/app/plugins/sijishe
curl -o /path/to/moviepilot/app/plugins/sijishe/__init__.py \
  https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/plugins.v2/sijishe/__init__.py
# 然后重启 MoviePilot
```

---

## 本地修复（如果你急着用）

如果远程仓库安装一直 404，我可以把文件直接放进你的 MoviePilot 插件目录。只需要告诉我：

1. MoviePilot 是宿主机安装还是 Docker？
2. 插件目录的完整路径（通常 Docker 映射是 `/path/to/moviepilot/app/plugins/`）

我直接帮你放进去，绕过市场安装。
