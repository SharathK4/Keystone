// Render every SVG in design/concepts to a PNG of the same size, with a local
// Chrome or Edge (playwright-core downloads no browser).
import { readdirSync, readFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { chromium } from 'playwright-core'

const here = dirname(fileURLToPath(import.meta.url))
const dir = join(here, 'concepts')
const candidates = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].filter(Boolean)
const executablePath = candidates.find((p) => existsSync(p))
if (!executablePath) throw new Error('No Chrome or Edge found; set CHROME_PATH')

const only = process.argv.slice(2)
const browser = await chromium.launch({ executablePath })
for (const name of readdirSync(dir).filter((f) => f.endsWith('.svg'))) {
  if (only.length && !only.some((o) => name.includes(o))) continue
  const svg = readFileSync(join(dir, name), 'utf8')
  const width = Number(/width="(\d+)"/.exec(svg)[1])
  const height = Number(/height="(\d+)"/.exec(svg)[1])
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 })
  await page.goto(pathToFileURL(join(dir, name)).href)
  await page.evaluate(() => document.fonts && document.fonts.ready)
  await page.screenshot({ path: join(dir, name.replace(/\.svg$/, '.png')) })
  await page.close()
  console.log('rendered', name, `${width}x${height}`)
}
await browser.close()
