// The persistent stage and its two casts (site cover · operator tool),
// extracted from SiteRoot unchanged (convergence W1).
//
// WHY IT MOVED: the drawing identity is now owned by DrawingIdentityProvider,
// and the stage's operator state has to READ that provider. SiteRoot is the
// component that decides which mode to mount, so it cannot also be the
// component that consumes the provider it mounts. Everything below is the
// same state, the same props and the same DOM SiteRoot rendered before —
// only `operatorDrawingId` and the upload promotion now come from the
// provider instead of local state.
//
// SiteRoot keeps the routing, the auth-callback deferral, the marketing
// redirect, the keyboard recasts and the inert/aria-hidden pass; `stageRef`
// is forwarded so those effects still reach this subtree's root element.
import { useCallback, useMemo, useRef, useState } from 'react'

import StageLayer from './StageLayer.jsx'
import LandingCast from './LandingCast.jsx'
import ToolCast from './ToolCast.jsx'
import { navigate } from './router.js'
import { WorkspaceControllerProvider } from '../controllers/WorkspaceControllerProvider.jsx'
import { operatorWorkspaceMount } from '../controllers/workspaceMount.js'
import { useDrawingIdentity } from '../drawing/DrawingIdentityProvider.jsx'

export default function StageScene({ scene, stageRef, publicDemo = false }) {
  const stageLayerRef = useRef(null)
  const [operatorIntake, setOperatorIntake] = useState(null)
  const [operatorViewMode, setOperatorViewMode] = useState('flat')
  const [operatorVisibleLayers, setOperatorVisibleLayers] = useState(null)
  const [operatorSelectedHandle, setOperatorSelectedHandle] = useState(null)
  const [operatorOverlay, setOperatorOverlay] = useState(null)
  const { drawingId: operatorDrawingId, setFromUpload } = useDrawingIdentity()

  // The mount shapes live in workspaceMount.js (convergence bug c): the
  // public-demo flag is the ONE reading SiteRoot made of `?demo`, handed down
  // rather than re-read here. Memoized on it exactly as before — the setters
  // are referentially stable.
  const mount = useMemo(() => operatorWorkspaceMount({
    publicDemo,
    onApplyIntake: setOperatorIntake,
    onResetSelection: () => setOperatorSelectedHandle(null),
  }), [publicDemo])

  // Upload promotion now routes through the provider, which also owns the
  // account-only "remember this drawing for the session" rule.
  const promoteOperatorDrawing = useCallback((receipt) => {
    setFromUpload(receipt)
  }, [setFromUpload])

  return (
    <WorkspaceControllerProvider
      drawingId={scene === 'tool' ? operatorDrawingId : 'rooftop_demo'}
      drawingOptions={mount.drawingOptions}
      retryNotFound={mount.retryNotFound}
    >
      <main className="stage-root" data-scene={scene} ref={stageRef} aria-label={scene === 'tool' ? 'Leaf operator workspace' : 'Leaf product overview'}>
        <StageLayer
          ref={stageLayerRef}
          intakeOverride={scene === 'tool' ? operatorIntake : null}
          viewMode={scene === 'tool' ? operatorViewMode : 'flat'}
          visibleLayers={scene === 'tool' ? operatorVisibleLayers : null}
          selectedHandle={scene === 'tool' ? operatorSelectedHandle : null}
          onSelectEntity={scene === 'tool' ? setOperatorSelectedHandle : undefined}
          overlay={scene === 'tool' ? operatorOverlay : null}
        />
        <LandingCast onTryTool={() => navigate('/try')} />
        <ToolCast
          active={scene === 'tool'}
          drawingId={operatorDrawingId}
          onDrawingReady={promoteOperatorDrawing}
          onFitDrawing={() => stageLayerRef.current?.fit()}
          onViewModeChange={setOperatorViewMode}
          onVisibleLayersChange={setOperatorVisibleLayers}
          selectedHandle={operatorSelectedHandle}
          onSelectedHandleChange={setOperatorSelectedHandle}
          onResultOverlayChange={setOperatorOverlay}
        />
      </main>
    </WorkspaceControllerProvider>
  )
}
