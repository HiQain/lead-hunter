Lead Hunter - No-Website Verified (All-in-One)
n8n workflow id: 1azmaXShFED6BG2C
Exported: 2026-11-12
Import: n8n > Workflows > Import from File
Webhooks: /webhook/lead-hunter-run?niche=&city=&state=  and  /webhook/lead-hunter-count
Pipeline: Serper Places -> Split (no-website + very-outdated) -> Scrape+Email -> Numverify -> AI Verify (Gemini) -> Dedupe vs whole sheet -> Write Leads_Final
Dedupe drops any lead whose email/phone/business already exists in Leads_Final (no repeats ever).