// README screenshots from the running app (API on :8000, Vite on :5173).
//
//   node scripts/screens.mjs            # writes ../docs/screens/*.png
//
// Uses the Chrome or Edge already installed; downloads no browser. Each shot is
// the real page: the lab round is written by the configured model and replayed.

import { chromium } from 'playwright-core'
import { existsSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const out = resolve(here, '../../docs/screens')
mkdirSync(out, { recursive: true })

const candidates = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
].filter(Boolean)
const executablePath = candidates.find((p) => existsSync(p))
if (!executablePath) throw new Error('No Chrome or Edge found; set CHROME_PATH')

const base = process.env.KEYSTONE_URL ?? 'http://localhost:5173'
const browser = await chromium.launch({ executablePath })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1.5 })

const settle = () => page.waitForTimeout(1200)

await page.goto(base, { waitUntil: 'networkidle' })
await page.waitForSelector('.lp')
await settle()
await page.screenshot({ path: `${out}/01-landing.png` })

// The rule lab: the people's rules, then the AI's rounds, then its winner.
await page.goto(`${base}/#lab`, { waitUntil: 'networkidle' })
await page.waitForFunction(
  () => document.querySelectorAll('.race__row.is-scored').length >= 6 && !document.querySelector('.race__row.is-testing'),
  null,
  { timeout: 120000 },
)
await page.click('.rl__write')
await page.waitForFunction(
  () => {
    const b = document.querySelector('.rl__write')
    return b && !b.disabled && document.querySelector('.rl__ring .wr')
  },
  null,
  { timeout: 300000 },
)
await settle()
await page.screenshot({ path: `${out}/02-rule-lab.png` })
await page.locator('.rl__win').scrollIntoViewIfNeeded()
await settle()
await page.screenshot({ path: `${out}/05-the-winner.png` })

// Pay the AI's rescue: the plan beside the search's, then the PayPal desk.
await page.click('.rl__pay')
await page.waitForSelector('.st__table', { timeout: 30000 })
await page.waitForTimeout(1500)
await page.locator('.ps__vs').scrollIntoViewIfNeeded()
await settle()
await page.screenshot({ path: `${out}/03-pay-with-paypal.png` })

await page.goto(`${base}/#proof`, { waitUntil: 'networkidle' })
await page.waitForSelector('.px__claims')
await settle()
await page.screenshot({ path: `${out}/04-what-we-checked.png` })

await browser.close()
console.log(`wrote 5 screenshots to ${out}`)
