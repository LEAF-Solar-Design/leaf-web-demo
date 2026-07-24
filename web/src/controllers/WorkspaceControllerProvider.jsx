import { createContext, useContext } from 'react'
import useConverseSessionController from './useConverseSessionController.js'

const WorkspaceControllerContext = createContext(null)

export function WorkspaceControllerProvider({ drawingId, retryNotFound = false, children }) {
  const converse = useConverseSessionController({ drawingId, retryNotFound })
  return (
    <WorkspaceControllerContext.Provider value={{ converse }}>
      {children}
    </WorkspaceControllerContext.Provider>
  )
}

export function useWorkspaceControllers() {
  const value = useContext(WorkspaceControllerContext)
  if (!value) throw new Error('useWorkspaceControllers must be used inside WorkspaceControllerProvider')
  return value
}
