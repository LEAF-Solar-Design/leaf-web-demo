import StudioClient from './StudioClient.jsx'

export const metadata = {
  title: 'Leaf Automation Studio',
  description: 'Build tools and apps in one shared workspace.',
  robots: { index: false, follow: false },
  icons: { icon: "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%20100%20100'%3E%3Crect%20x='26'%20y='26'%20width='48'%20height='48'%20rx='8'%20transform='rotate(45%2050%2050)'%20fill='%231e6b45'/%3E%3C/svg%3E" },
}

export const viewport = { width: 'device-width', initialScale: 1, viewportFit: 'cover' }

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <head>
        <link rel="preload" href="/fonts/barlow-400-latin.woff2" as="font" type="font/woff2" crossOrigin="anonymous" />
        <link rel="preload" href="/fonts/ibm-plex-mono-400-latin.woff2" as="font" type="font/woff2" crossOrigin="anonymous" />
      </head>
      <body><div id="root"><StudioClient /></div>{children}</body>
    </html>
  )
}
