# 接线复勘「页面」验收镜像：仅用于 verify-web 服务，不进入 app 镜像。
# 用 jsdom 真实加载 app 返回的页面并驱动交互，结论只来自真实接口。
FROM node:20-slim

WORKDIR /verify

COPY verify/page/package.json verify/page/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY verify/page/page_acceptance.mjs ./page_acceptance.mjs

CMD ["node", "page_acceptance.mjs"]
