"""Bound request bodies before JSON decoding, including chunked public submissions."""
from starlette.responses import JSONResponse


class WorkflowBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if scope['type'] != 'http' or scope.get('method') not in {'POST', 'PATCH'} or not path.startswith(('/api/v1/inquiries', '/api/v1/tenants', '/api/v1/staff', '/api/v1/properties', '/api/v1/rooms', '/api/v1/buildings', '/api/v1/floors', '/api/v1/beds')):
            return await self.app(scope, receive, send)
        maximum = 16_384 if path.startswith('/api/v1/inquiries') else 7_100_000
        if path.startswith(('/api/v1/properties','/api/v1/rooms','/api/v1/buildings','/api/v1/floors','/api/v1/beds')):
            maximum = (4 * 1024 * 1024 * 12 + 65536) if path.endswith('/images') else 32768
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > maximum:
                return await JSONResponse({'error': {'message': 'Request body is too large.'}}, status_code=413)(scope, receive, send)
            chunks.append(message)
            if not message.get('more_body', False):
                break
        position = 0

        async def bounded_receive():
            nonlocal position
            if position < len(chunks):
                result = chunks[position]
                position += 1
                return result
            return await receive()
        await self.app(scope, bounded_receive, send)
