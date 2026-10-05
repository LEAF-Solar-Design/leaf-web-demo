import { useEffect, useState } from 'react'

function readOnline() {
  return typeof navigator === 'undefined' || navigator.onLine !== false
}

export default function useOnline() {
  const [online, setOnline] = useState(readOnline)

  useEffect(() => {
    const sync = () => setOnline(readOnline())
    window.addEventListener('online', sync)
    window.addEventListener('offline', sync)
    sync()
    return () => {
      window.removeEventListener('online', sync)
      window.removeEventListener('offline', sync)
    }
  }, [])

  return online
}
