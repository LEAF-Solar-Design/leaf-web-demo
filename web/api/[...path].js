/** Terminal boundary: the Vercel web origin does not host the platform API. */
export default function handler(_request, response) {
  response.setHeader('Content-Type', 'application/json; charset=utf-8')
  response.setHeader('Cache-Control', 'no-store')
  return response.status(404).json({
    ok: false,
    error: 'web origin does not serve API routes',
  })
}
