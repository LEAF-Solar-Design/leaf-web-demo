// @vitest-environment jsdom
import { it, expect } from 'vitest'
import { copyChartState, attachChartStateBridge } from './privatePreviewState.js'
const state=()=>({version:1,range:'all',scale:'Log',reference:'2023-01-01',averaging:56,timeAxis:'expanded2026',series:['a'],groups:['g']})
it('copies state without changing defaults or aliases',()=>{const input=state(),copy=copyChartState(input);input.series.push('b');expect(copy.series).toEqual(['a']);expect(copy.averaging).toBe(56)})
it.each(['version','unknown','duplicate','oversize','scale'])('refuses %s state',kind=>{const input=state();if(kind==='version')input.version=2;if(kind==='unknown')input.extra=true;if(kind==='duplicate')input.series=['a','a'];if(kind==='oversize')input.series=Array.from({length:200},(_,n)=>String(n).padEnd(200,'x'));if(kind==='scale')input.scale='remote';expect(()=>copyChartState(input)).toThrow()})
it('escapes state strings inside the bridge script',()=>{const input=state();input.series=['</script><script>not code</script>'];const html=attachChartStateBridge('<html><head></head><body>safe</body></html>',{nonce:'fixture',jobId:'j',artifactId:'a'},input);const doc=new DOMParser().parseFromString(html,'text/html');expect(doc.scripts).toHaveLength(1);expect(doc.scripts[0].textContent).toContain('\\u003c');expect(doc.scripts[0].textContent).not.toContain('</script>')})
