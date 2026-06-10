import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import './index.css'
import Upload from './screens/Upload'
import Batch from './screens/Batch'
import Detail from './screens/Detail'

const router = createBrowserRouter([
  { path: '/', element: <Upload /> },
  { path: '/batches/:batchId', element: <Batch /> },
  { path: '/records/:recordId', element: <Detail /> },
])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
)
