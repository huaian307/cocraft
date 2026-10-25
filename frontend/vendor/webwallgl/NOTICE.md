# 第三方声明 · WebWallGL

本目录不是 cocraft 自己的代码，是**原样引入**的第三方构建产物，用来在面板里实时渲染
Wallpaper Engine 的 **Scene（场景）** 壁纸。

| 项 | 值 |
| --- | --- |
| 组件 | WebWallGL（浏览器端 Wallpaper Engine 渲染核心，WebGL2） |
| 上游 | https://github.com/oneincase/webwallgl |
| npm | `webwallgl@3.0.0` |
| 取包地址 | https://cdn.jsdelivr.net/npm/webwallgl@3.0.0/webwallgl.global.min.js |
| 取包时间 | 2026-10-24 |
| 文件 | `webwallgl.global.min.js` · **1,106,453 B** |
| SHA-256 | `2f86ab955f3377e2fa2cfd5ffa72f5813fe8f6e4a272295fed36f4c1e626b1e5` |
| 许可 | **MIT**（见同目录 `LICENSE`，Copyright (c) 2026 oneincase） |
| 是否修改 | **未修改**（原样使用）。要改就 fork，并在此处写明改了什么 |

## 用法（我们这边的接入点）

```js
// 只在需要时才注入，不拖慢启动
const src = "/vendor/webwallgl/webwallgl.global.min.js";
// 挂载：baseUrl 指向“一个壁纸目录”，库自己取 project.json / scene.pkg
WebWallGL.mount(canvas, { source: WebWallGL.httpSource("/we/<创意工坊 id>/") });
```

- 只支持 **`kind == "scene"`** 的壁纸；视频壁纸仍走原来的 `<video>` 播放。
- **不打包 mediabunny**（`webwallgl.global.min.js` 里 0 次引用）—— 那是 MPL-2.0 的依赖，
  只有 `.mjs` 形态才 `import` 它。所以我们只吃 MIT 这一项义务。
- 工坊壁纸本身（画面、音频、字体）版权归各自作者，**不随本仓库分发**，只在用户本机读取。
- Wallpaper Engine 是其权利人的产品名；本项目只是**兼容其壁纸格式**，非官方、无背书。

## 升级步骤

1. 换 `webwallgl.global.min.js`；
2. 更新上表的**版本 / 取包时间 / 字节数 / SHA-256**；
3. 跑一次真机验收（挂 1~2 张场景壁纸，确认没报错、能出画面）。
