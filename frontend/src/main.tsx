import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { KeystonePage } from './components/KeystonePage'
import './styles/global.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <KeystonePage />
  </StrictMode>,
)
