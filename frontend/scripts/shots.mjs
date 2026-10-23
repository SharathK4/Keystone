// Screenshots of the landing and every analysis, at desktop and phone sizes.
//   node scripts/shots.mjs --out=<dir> [--only=network,offer] [--sizes=1440,390]
// The analyses are scrolling overlays; for the capture they are laid out at full
// length so one image holds the whole view.
import { mkdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { chromium } from 'playwright-core'

const args = Object.fromEntries(process.argv.slice(2).map((a) => a.replace(/^--/, '').split('=')))
const out = args.out ?? '../docs/screens'
mkdirSync(out, { recursive: true })
const base = args.base ?? 'http://localhost:5173'
const chrome = [process.env.CHROME_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', '/usr/bin/google-chrome'].filter(Boolean).find((p) => existsSync(p))
const sizes = (args.sizes ?? '1440,390').split(',').map((w) => ({ w: Number(w), h: Number(w) >= 1000 ? 900 : 844 }))
const views = (args.only ?? 'landing,network,exposure,intervention,offer,proof').split(',')
const UNFOLD = `.xa{position:absolute!important;inset:0 0 auto 0!important;height:auto!important}
.xa__shell{max-height:none!important;height:auto!important;overflow:visible!important}
html,body{overflow:visible!important;height:auto!important}`

const browser = await chromium.launch({ executablePath: chrome })
for (const { w, h } of sizes) {
  for (const v of views) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, reducedMotion: 'reduce' })
    const page = await ctx.newPage()
    await page.goto(v === 'landing' ? `${base}/` : `${base}/#${v}`)
    await page.waitForLoadState('networkidle').catch(() => {})
    await page.waitForTimeout(900)
    if (v === 'offer') {
      const btn = page.getByRole('button', { name: /Review and approve/ })
      if (await btn.count()) { await btn.click(); await page.waitForTimeout(900) }
    }
    if (v !== 'landing') await page.addStyleTag({ content: UNFOLD })
    await page.waitForTimeout(300)
    const name = `${v}-${w}.png`
    await page.screenshot({ path: join(out, name), fullPage: true })
    console.log('shot', name)
    await ctx.close()
  }
}
await browser.close()
