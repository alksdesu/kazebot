# Clonoth Web 前端

基于 React + Vite + Tailwind CSS 的 Web 管理界面。由 Supervisor 以静态文件形式挂载，提供对话、节点管理、模型配置、审批等功能。

## 目录结构

```
adapters/web/
└── frontend/
    ├── src/
    │   ├── api/            # Supervisor API 客户端；request.ts 统一带令牌、超时和错误
    │   ├── components/     # React 组件
    │   │   ├── chat/       # 对话界面
    │   │   └── settings/   # 设置面板（节点、工具、MCP、定时任务等）
    │   ├── console/        # 控制台各页（人格、模型参数、记忆、平台配置等）
    │   ├── features/       # 独立功能页（执行、资料、社区等）
    │   ├── hooks/          # 共享 hooks
    │   ├── instances/      # 多开实例：路径前缀与实例目录
    │   ├── platforms/      # 平台清单（读后端公布的平台描述）
    │   ├── plugins/        # 前端插件 SDK（说明见 plugins/README.md）
    │   ├── store/          # Zustand 状态
    │   ├── views/          # 工作区视图注册
    │   └── App.tsx         # 应用外壳与视图切换
    ├── dist/               # 构建产物（由 Supervisor 挂载）
    ├── vite.config.ts      # Vite 配置，base 为相对路径 ./
    ├── package.json
    └── tsconfig.json
```

## 前置依赖

- Node.js 18+
- npm 或 pnpm

## 构建步骤

### 1. 安装依赖

```bash
cd adapters/web/frontend
npm install
```

### 2. 开发模式

```bash
npm run dev
# 访问 http://localhost:5173/
```

前端的 API 和 WebSocket 都发往页面所在源的 `/v1/`（多开时带实例前缀），开发服务器本身没有配置代理。要连真后端调试，请在 `vite.config.ts` 的 `server.proxy` 里把 `/v1` 转到 Supervisor（默认 `http://127.0.0.1:8765`，记得开 `ws: true`），或直接 `npm run build` 后用 Supervisor 挂载的 `/web/` 访问。

### 3. 生产构建

```bash
npm run build
```

构建产物输出到 `dist/` 目录。

### 4. 测试

```bash
# 运行全部测试
npm test

# 单次运行
npm run test:run
```

## 部署方式

### 方式一：Supervisor 自动挂载（默认）

Supervisor 启动时会自动检测 `adapters/web/frontend/dist/` 目录。如果存在，将其挂载到 `/web/` 路径：

```
http://{supervisor_host}:{supervisor_port}/web/
```

无需额外配置。只需确保 `dist/` 目录存在即可。

### 方式二：独立 HTTP 服务

也可以用 nginx 或其他 Web 服务器单独托管 `dist/` 目录。需要配置反向代理，将 `/v1/` 路径转发到 Supervisor API：

```nginx
server {
    listen 80;
    server_name clonoth.example.com;

    location /web/ {
        alias /path/to/clonoth/adapters/web/frontend/dist/;
        try_files $uri $uri/ /web/index.html;
    }

    location /v1/ {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

## 生产部署注意事项

### 构建产物同步

在多实例部署中，应在 `clonoth_original` 中执行 `npm run build`，然后将整个 `dist/` 目录（包括 `index.html`）复制到各生产实例。不要在生产实例中单独构建，因为每次 Vite 构建会生成不同的文件名哈希，导致 `index.html` 引用的文件名与实际文件不匹配。

```bash
# 正确做法
cd /path/to/clonoth_original/adapters/web/frontend
npm run build
cp -r dist/ /path/to/production/adapters/web/frontend/dist/
```

### Admin Token

设置面板中的部分功能（节点编辑、工具管理、模型配置、引擎重启等）需要 Admin Token。Token 存储在 `data/.admin_token` 文件中，首次使用时在前端设置页面输入。

### base 路径

`vite.config.ts` 中 `base` 是相对路径 `./`：多开时每个实例挂在自己的路径前缀下（如 `/i/<实例>/web/`），同一份 `dist/` 的资源都跟着页面所在路径解析，不用为每个实例单独构建。API 与 WebSocket 的前缀由页面路径里最后一个 `/web/` 之前的部分推出（`src/instances/paths.ts` 的 `MOUNT`），所以 Supervisor 必须把控制台挂在以 `/web/` 结尾的路径上。

## 技术栈

- React 18
- Vite 6
- Tailwind CSS 4
- Zustand（状态管理）
- js-yaml（配置文件解析）
- react-markdown + remark-gfm（Markdown 渲染）
- Vitest + Testing Library（测试）
