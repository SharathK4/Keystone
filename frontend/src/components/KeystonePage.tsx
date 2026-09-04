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
import { FourPanelGrid } from './FourPanelGrid'
import { NetworkExpanded } from './expanded/NetworkExpanded'
import { ExposureExpanded } from './expanded/ExposureExpanded'
import { InterventionExpanded } from './expanded/InterventionExpanded'
import { OfferExpanded } from './expanded/OfferExpanded'
import { Failure, Loading } from './SystemState'
import { LightColumn } from './reactbits/LightColumn'
import type { PanelId } from './ExpandedAnalysis'
import './keystone-page.css'

const PANELS: PanelId[] = ['network', 'exposure', 'intervention', 'offer']

function panelFromHash(): PanelId | null {
  const id = window.location.hash.replace('#', '') as PanelId
  return PANELS.includes(id) ? id : null
}

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

  useEffect(() => {
    const onHash = () => setOpen(panelFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    const next = open ? `#${open}` : ' '
    if (window.location.hash !== (open ? `#${open}` : '')) {
      window.history.replaceState(null, '', next === ' ' ? window.location.pathname : next)
    }
  }, [open])

  const close = useCallback(() => setOpen(null), [])

  return (
    <main className={`ks${open ? ' is-open' : ''}`}>
      <div className="ks__light">
        <LightColumn intensity={1} />
      </div>

      <header className="ks__masthead">
        <h1 className="ks__wordmark">Keystone</h1>
        {structure ? (
          <p className="ks__standfirst">
            A liquidity shock at one merchant, followed all the way to the capital that
            stops it.
          </p>
        ) : null}
      </header>

      {phase === 'loading' ? <Loading /> : null}
      {phase === 'failed' && error ? <Failure message={error.message} path={error.path} /> : null}

      {phase === 'ready' && structure ? (
        <FourPanelGrid
          structure={structure}
          scenario={scenario}
          scenarioBusy={scenarioBusy}
          onOpen={setOpen}
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
          scenario={scenario}
          scenarios={structure.scenarios}
          selectedId={scenarioId}
          busy={scenarioBusy}
          onSelect={selectScenario}
          onClose={close}
        />
      ) : null}

      {open === 'intervention' && structure ? (
        <InterventionExpanded
          scenario={scenario}
          merchants={structure.merchants}
          systemic={structure.systemic}
          execution={structure.execution}
          busy={scenarioBusy}
          onClose={close}
        />
      ) : null}

      {open === 'offer' && structure ? (
        <OfferExpanded
          scenario={scenario}
          execution={structure.execution}
          busy={scenarioBusy}
          onClose={close}
        />
      ) : null}
    </main>
  )
}
