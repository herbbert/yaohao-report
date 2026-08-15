// Cloudflare Worker: 广州摇号官网反向代理
// 作用：中转 GitHub Actions 对 jtzl.jtj.gz.gov.cn 的请求，绕过海外 IP 403 限制
// 部署后 Cloudflare 边缘节点（中国有节点）会代为访问目标网站

const TARGET_HOST = "jtzl.jtj.gz.gov.cn";

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // 健康检查
    if (url.pathname === "/health") {
      return new Response("ok", { status: 200 });
    }

    // 构造目标 URL：把 Worker 域名替换为目标域名
    const targetUrl = `https://${TARGET_HOST}${url.pathname}${url.search}`;

    // 转发请求，保留原始 headers 但修改 Host 和 Referer
    const headers = new Headers(request.headers);
    headers.set("Host", TARGET_HOST);
    headers.set("Referer", `https://${TARGET_HOST}/`);
    headers.set("Origin", `https://${TARGET_HOST}`);
    // 删除可能暴露代理的 header
    headers.delete("cf-connecting-ip");
    headers.delete("cf-ray");
    headers.delete("cf-visitor");
    headers.delete("cf-worker");

    try {
      const response = await fetch(targetUrl, {
        method: request.method,
        headers: headers,
        body: request.method !== "GET" && request.method !== "HEAD" ? request.body : undefined,
        redirect: "follow",
      });

      // 返回响应，添加 CORS 头
      const respHeaders = new Headers(response.headers);
      respHeaders.set("Access-Control-Allow-Origin", "*");
      respHeaders.set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");
      respHeaders.set("Access-Control-Allow-Headers", "*");
      // 移除可能导致问题的安全头
      respHeaders.delete("content-security-policy");
      respHeaders.delete("x-frame-options");

      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: respHeaders,
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
  },
};
