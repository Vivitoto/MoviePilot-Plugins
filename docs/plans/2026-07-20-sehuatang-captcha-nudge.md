# 98 签到验证码微调按钮实施计划

1. 为验证码页面补充最小静态回归测试，先验证当前页面缺少 slide/rotate 微调控件与处理函数。
2. 在 `captcha_server.py` 中增加移动端友好的微调按钮样式和模板控件。
3. 在现有 slide JS 闭包内实现 `nudgeSlide(delta)`，复用 clamp/render 更新原图坐标答案。
4. 在现有 rotate JS 闭包内实现 `nudgeRotate(delta)`，同步 range 并复用 renderAngle 更新角度答案。
5. 将插件版本提升到 `1.0.18`，同步 `package.v2.json` 并按规则保留最近 6 条历史。
6. 运行回归测试、Python 编译、JSON 解析和版本一致性检查；审查 diff，确保签到主流程未改。
