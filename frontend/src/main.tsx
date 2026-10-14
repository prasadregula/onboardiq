import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import './index.css'
import Shell from './components/Shell'
import Queue from './pages/Queue'
import CasePage from './pages/CasePage'
import Cases from './pages/Cases'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<Queue />} />
          <Route path="cases" element={<Cases />} />
          <Route path="cases/:id" element={<CasePage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </React.StrictMode>,
)
