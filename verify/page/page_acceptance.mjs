// 接线复勘页面验收：用 jsdom 真实加载服务端返回的 index.html，
// 点击按钮、填写表格、读取渲染后的结论；所有结论只来自真实接口。
//
// 覆盖：
//   1) 旧双路规划页面回归（贪心反例 -> 总延迟 12）；
//   2) 复勘局部贪心误配反例（页面内置示例：延迟全 3，整体择优才得 3 段完全匹配）；
//   3) 复勘对称歧义（同优映射数、必然/可选目标、规范映射稳定）；
//   4) 锁定冲突（400 定位到 locks 下标，并清除本次结论）；
//   5) 编辑即清除旧结论。
import { JSDOM } from "jsdom";

const BASE = process.env.APP_URL || "http://app:8080";
const failures = [];
function check(name, cond, detail = "") {
  console.log(
    `[${cond ? "PASS" : "FAIL"}] ${name}` +
      (!cond && detail ? ` -- ${String(detail).slice(0, 300)}` : ""),
  );
  if (!cond) failures.push(name);
}

async function getHtml() {
  const resp = await fetch(BASE + "/");
  if (!resp.ok) throw new Error("首页不可达: " + resp.status);
  return await resp.text();
}

async function main() {
  const html = await getHtml();
  const dom = new JSDOM(html, {
    url: BASE + "/",
    runScripts: "dangerously",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  // 让页面脚本里的 fetch 打到真实 app 服务。
  window.fetch = (url, opts) =>
    globalThis.fetch(new URL(url, BASE).href, opts);
  window.AbortController = globalThis.AbortController;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function waitText(el, token, ms = 10000) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      if (el.textContent.includes(token)) return true;
      await sleep(40);
    }
    return false;
  }
  const setSegInputs = (tbody, i, vals) => {
    const tr = tbody.querySelectorAll("tr")[i];
    for (const [f, v] of Object.entries(vals)) {
      const inp = tr.querySelector(`[data-f="${f}"]`);
      inp.value = String(v);
      inp.dispatchEvent(new window.Event("input", { bubbles: true }));
    }
  };
  const delRow = (tbody, i) =>
    tbody.querySelectorAll("tr")[i].querySelector(".del-row").click();

  await sleep(100); // 等脚本填充示例
  const doc = window.document;

  /* 1) 旧双路规划回归：页面默认示例是贪心反例，应得最小总延迟 12。 */
  doc.getElementById("submit").click();
  const rp = doc.getElementById("result");
  check("旧双路：出现结果", await waitText(rp, "最小总延迟"));
  check("旧双路：最小总延迟 12", rp.textContent.includes("12"));

  /* 2) 复勘局部贪心误配反例（页面内置示例），整体择优。 */
  doc.getElementById("submit-reconcile").click();
  const rrc = doc.getElementById("reconcile-result");
  check("复勘：出现规范映射", await waitText(rrc, "规范映射"));
  let flat = rrc.textContent.replace(/\s+/g, " ");
  check("复勘贪心反例：同优映射唯一（=1）", flat.includes("全部同优映射数：1"));
  check("复勘贪心反例：x→A", /x\s*→\s*A/.test(flat), flat);
  check("复勘贪心反例：y→C（就近会误判为 B）", /y\s*→\s*C/.test(flat), flat);
  check("复勘贪心反例：z→B", /z\s*→\s*B/.test(flat), flat);
  check("复勘贪心反例：完全匹配 3 段", flat.includes("共 3 段"), flat);
  check("复勘贪心反例：延迟差之和为 0", flat.includes("之和 = 0"), flat);
  check("复勘贪心反例：两侧未匹配段均为 0",
        flat.includes("已批准侧未匹配（0）") &&
        flat.includes("复勘侧未匹配（0）"), flat);

  /* 3) 对称歧义：中心 C + 两片同构叶子 L1/L2。 */
  const ab = doc.getElementById("approved-body");
  const sb = doc.getElementById("survey-body");
  delRow(ab, 2);
  delRow(sb, 2);
  setSegInputs(ab, 0, { id: "a1", from: "C", to: "L1", delay: 2 });
  setSegInputs(ab, 1, { id: "a2", from: "C", to: "L2", delay: 2 });
  setSegInputs(sb, 0, { id: "f1", from: "c", to: "p", delay: 2 });
  setSegInputs(sb, 1, { id: "f2", from: "c", to: "q", delay: 2 });
  doc.getElementById("submit-reconcile").click();
  check("对称：同优映射数 = 2", await waitText(rrc, "全部同优映射数：2"));
  flat = rrc.textContent.replace(/\s+/g, " ");
  check("对称：规范映射 p→L1", /p\s*→\s*L1/.test(flat), flat);
  check("对称：q→L2", /q\s*→\s*L2/.test(flat), flat);
  check("对称：c 必然对应 C", /c\s*→\s*C/.test(flat) && flat.includes("必然"));
  check("对称：p/q 标记为可选且列出 L1、L2", flat.includes("可选"));

  /* 4) 锁定冲突：同一原节点锁给两个代号 => 400 定位并清除本次结论。 */
  doc.getElementById("add-lock").click();
  doc.getElementById("add-lock").click();
  const lockRows = doc.querySelectorAll("#lock-rows .lock-grid");
  lockRows[0].querySelector('[data-l="code"]').value = "p";
  lockRows[0].querySelector('[data-l="node"]').value = "L1";
  lockRows[1].querySelector('[data-l="code"]').value = "q";
  lockRows[1].querySelector('[data-l="node"]').value = "L1";
  doc.getElementById("submit-reconcile").click();
  check("锁定冲突：错误徽标（本次结论已清除）",
        await waitText(rrc, "本次结论已清除"));
  check("锁定冲突：定位到 locks[1]",
        rrc.textContent.includes("locks[1]"), rrc.textContent);

  /* 5) 复勘区任意编辑即清除结论。 */
  sb.querySelectorAll("tr")[0]
    .querySelector('[data-f="delay"]')
    .dispatchEvent(new window.Event("input", { bubbles: true }));
  check("编辑即清除复勘结论", rrc.className.includes("hidden"));

  console.log("\n" + "=".repeat(50));
  if (failures.length) {
    console.log(`页面验收失败：${failures.length} -> ${failures.join(", ")}`);
    process.exit(1);
  }
  console.log("页面验收全部通过");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
