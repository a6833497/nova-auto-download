/**
 * 将api-download.mjs生成的JSON文件导入数据库
 * 只处理"日主播工作数据"tab（这是ingest.ts需要的主数据）
 * 
 * 用法: node ingest-api-json.mjs [日期目录]
 * 示例: node ingest-api-json.mjs 2026-04-27
 */
import fs from "fs";
import path from "path";
import { execSync } from "child_process";

const dateArg = process.argv[2] || (() => {
  const d = new Date(); d.setDate(d.getDate() - 1);
  return d.toISOString().slice(0, 10);
})();

const dataDir = `/home/ubuntu/nova-data/upload-staging/daily/${dateArg}`;
const API_DIR = "/home/ubuntu/nova-dashboard-deploy-final/api";

console.log(`[ingest-api-json] 日期: ${dateArg}`);
console.log(`[ingest-api-json] 目录: ${dataDir}`);

// 找到所有"日主播工作数据"的JSON文件
const jsonFiles = fs.readdirSync(dataDir)
  .filter(f => f.endsWith(".json") && f.includes("日主播工作数据"))
  .map(f => path.join(dataDir, f));

if (jsonFiles.length === 0) {
  console.log("[ingest-api-json] 未找到日主播工作数据JSON文件");
  process.exit(1);
}

console.log(`[ingest-api-json] 找到 ${jsonFiles.length} 个JSON文件:`);
jsonFiles.forEach(f => console.log(`  ${path.basename(f)}`));

// 将JSON转为ingest能接受的格式（数组of对象）并保存为临时JSON
let totalRows = 0;
const allRecords = [];

for (const jsonFile of jsonFiles) {
  const data = JSON.parse(fs.readFileSync(jsonFile, "utf-8"));
  const rows = data.rows || [];
  
  // 过滤：只保留conversation_type=total的行（汇总行）
  const filtered = rows.filter(r => 
    r.conversation_type === "total" && r.streamer_is_off === "N"
  );
  
  console.log(`  ${path.basename(jsonFile)}: ${rows.length}行 → 过滤后${filtered.length}行`);
  allRecords.push(...filtered);
  totalRows += filtered.length;
}

console.log(`[ingest-api-json] 总计 ${totalRows} 条记录`);

// 保存为临时JSON文件给ingest.ts处理
const tmpFile = `/tmp/api-ingest-${dateArg}.json`;
fs.writeFileSync(tmpFile, JSON.stringify(allRecords, null, 0));
console.log(`[ingest-api-json] 临时文件: ${tmpFile} (${Math.round(fs.statSync(tmpFile).size/1024)}KB)`);

// 调用ingest
console.log(`[ingest-api-json] 开始导入...`);
try {
  const result = execSync(
    `cd ${API_DIR} && npx tsx -e "
      import { ingestFiles } from ./src/services/ingest.js;
      const result = await ingestFiles([]);
      console.log(processed= + result.filesProcessed +  upserted= + result.metricsUpserted +  skipped= + result.skippedFiles);
      process.exit(0);
    "`,
    { timeout: 300000, encoding: "utf-8" }
  );
  console.log(result.trim().split("\n").slice(-3).join("\n"));
} catch (e) {
  console.error("[ingest-api-json] 导入失败:", e.message?.slice(0, 200));
}

// 清理临时文件
fs.unlinkSync(tmpFile);
console.log("[ingest-api-json] 完成");
