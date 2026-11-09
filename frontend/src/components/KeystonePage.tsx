/**
 * The page.
 *
 * A masthead and four panels. The reading order is the product's argument:
 *
 *   understand → see → decide → act
 *
 * Each panel is a teaser for the system behind it. Opening one does not
 * navigate: the composition stays where it is, recedes, and the analysis comes
 * forward on top of it, so closing returns the reader exactly where they were.
 * The four-panel grid is the anchor and never goes away.
 *
 * Scenario selection lives in panel 2 because that is the first panel whose
 * content depends on it; panels 3 and 4 follow the same selection, and panel 1
 * keeps the network. That is why changing scenario does not feel like a reload.
 */

import { useCallback, useEffect, useState } from 'react'
import { useKeystoneData } from '../api/useKeystoneData'
import { RuleLab } from '../lab/RuleLab'
import { FourPanelGrid } from './FourPanelGrid'
import { NetworkExpanded } from './expanded/NetworkExpanded'
import { ExposureExpanded } from './expanded/ExposureExpanded'
import { InterventionExpanded } from './expanded/InterventionExpanded'
import { OfferExpanded } from './expanded/OfferExpanded'
import { ProofExpanded } from './expanded/ProofExpanded'
import { Failure, Loading } from './SystemState'
import { LightColumn } from './reactbits/LightColumn'
import type { PanelId } from './ExpandedAnalysis'
import { declaredRate, setMoneyDisplay, type Currency } from '../lib/format'
import './keystone-page.css'

const PANELS: PanelId[] = ['network', 'exposure', 'intervention', 'offer', 'proof']

const CURRENCY_KEY = 'keystone.currency'

/** A reader's earlier choice, if this browser kept one. Never required. */
function storedCurrency(): Currency | null {
  try {
    const v = window.localStorage.getItem(CURRENCY_KEY)
    return v === 'USD' || v === 'INR' ? v : null
  } catch {
    return null
  }
}

function panelFromHash(): PanelId | null {
  const id = window.location.hash.replace('#', '') as PanelId
  return PANELS.includes(id) ? id : null
}

const labFromHash = () => window.location.hash === '#lab'

export function KeystonePage() {
  const {
    phase,
    error,
    structure,
    scenario,
    scenarioId,
    scenarioBusy,
    selectScenario,
    merchant,
    merchantId,
    selectMerchant,
  } = useKeystoneData()

  // The open analysis is reflected in the hash, so a reader can send someone
  // "the intervention view" rather than "open it and click the third panel".
  // Still one page and still a state transition - the hash only seeds it.
  const [open, setOpen] = useState<PanelId | null>(() => panelFromHash())
  const [labOpen, setLabOpen] = useState(labFromHash)

  useEffect(() => {
    const onHash = () => {
      setOpen(panelFromHash())
      setLabOpen(labFromHash())
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    const next = labOpen ? '#lab' : open ? `#${open}` : ''
    if (window.location.hash !== next) {
      window.history.replaceState(null, '', next || window.location.pathname)
    }
  }, [open, labOpen])

  const closeLab = useCallback(() => setLabOpen(false), [])

  const [payIntent, setPayIntent] = useState(false)
  const close = useCallback(() => {
    setOpen(null)
    setPayIntent(false)
  }, [])

  const shown = scenario
  const pickScenario = selectScenario

  // Money is printed in the reader's currency, defaulting to the server's. It
  // is set here, above every panel, before any of them renders a figure.
  const [chosen, setChosen] = useState<Currency | null>(() => storedCurrency())
  const display = structure?.display ?? null
  const currency: Currency = display ? (chosen ?? display.currency) : 'INR'
  if (display) {
    setMoneyDisplay({
      currency,
      usdPerInr: Number(display.usd_per_inr),
      sandboxRatio: display.sandbox_ratio ? Number(display.sandbox_ratio) : null,
    })
  }
  const flipCurrency = () => {
    const next: Currency = currency === 'USD' ? 'INR' : 'USD'
    setChosen(next)
    try {
      window.localStorage.setItem(CURRENCY_KEY, next)
    } catch {
      // A private window keeps the choice for this visit only.
    }
  }

  return (
    <main className={`ks${open ? ' is-open' : ''}`}>
      <div className="ks__light">
        <LightColumn intensity={1} />
      </div>

      <header className="ks__masthead">
        <h1 className="ks__wordmark">Keystone</h1>
        {structure ? (
          <>
            <p className="ks__standfirst">
              When one merchant misses a payment, others start missing theirs. Keystone finds
              the few merchants to fund that stop it, and pays them through PayPal.
            </p>
            {display ? (
              <p className="ks__units">
                {currency === 'USD'
                  ? `Amounts in US dollars at a declared ${declaredRate()}, not a market rate.`
                  : 'Amounts in rupees, the network’s own currency.'}{' '}
                <button type="button" className="ks__units-switch" onClick={flipCurrency}>
                  {currency === 'USD' ? 'Show rupees' : 'Show US dollars'}
                </button>
              </p>
            ) : null}
          </>
        ) : null}
      </header>


      {phase === 'loading' ? <Loading /> : null}
      {phase === 'failed' && error ? <Failure message={error.message} path={error.path} /> : null}

      {phase === 'ready' && structure ? (
        <FourPanelGrid
          structure={structure}
          scenario={shown}
          scenarioBusy={scenarioBusy}
          onOpen={setOpen}
          onOpenLab={() => setLabOpen(true)}
        />
      ) : null}

      {/* ---- the analysis behind whichever panel was opened ---- */}
      {open === 'network' && structure ? (
        <NetworkExpanded
          network={structure.network}
          merchants={structure.merchants}
          dependencies={structure.dependencies}
          systemic={structure.systemic}
          detail={merchant}
          selectedId={merchantId}
          onSelect={selectMerchant}
          onClose={close}
        />
      ) : null}

      {open === 'exposure' && structure ? (
        <ExposureExpanded
          merchants={structure.merchants}
          scenario={shown}
          scenarios={structure.scenarios}
          selectedId={scenarioId}
          busy={scenarioBusy}
          onSelect={pickScenario}
          onClose={close}
        />
      ) : null}

      {open === 'intervention' && structure ? (
        <InterventionExpanded
          scenario={shown}
          merchants={structure.merchants}
          systemic={structure.systemic}
          execution={structure.execution}
          busy={scenarioBusy}
          onClose={close}
        />
      ) : null}

      {open === 'offer' && structure ? (
        <OfferExpanded
          scenario={shown}
          execution={structure.execution}
          busy={scenarioBusy}
          reviewFirst={payIntent}
          onClose={close}
        />
      ) : null}

      {open === 'proof' && structure ? <ProofExpanded onClose={close} /> : null}

      {labOpen ? <RuleLab onClose={closeLab} /> : null}
    </main>
  )
}
