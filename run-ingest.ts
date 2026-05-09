import { ingestFiles } from "/home/ubuntu/nova-dashboard-deploy-final/api/src/services/ingest.js";
const file = process.argv[2];
if (!file) { console.error("需要文件路径参数"); process.exit(1); }
async function main() {
  console.log("导入:", file);
  const result = await ingestFiles([file]);
  console.log("processed=" + result.filesProcessed + " upserted=" + result.metricsUpserted + " skipped=" + result.skippedFiles);
  process.exit(0);
}
main().catch(e => { console.error(e.message?.slice(0, 200)); process.exit(1); });
