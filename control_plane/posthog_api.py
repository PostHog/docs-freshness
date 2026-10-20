import json
import urllib.error
import urllib.parse
import urllib.request


class PostHogApiError(RuntimeError):
    pass


class PostHogApi:
    def __init__(self, host, project_id, personal_api_key):
        self.host = host.rstrip('/')
        self.project_path = f'/api/projects/{urllib.parse.quote(project_id, safe="")}'
        self.personal_api_key = personal_api_key

    def request(self, method, url, payload=None):
        if not url.startswith(self.host + '/'):
            raise PostHogApiError(f'Refusing to send the API key to {url}, which is outside {self.host}')
        body = None if payload is None else json.dumps(payload).encode()
        headers = {'Authorization': f'Bearer {self.personal_api_key}', 'Content-Type': 'application/json'}
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as failure:
            detail = failure.read().decode(errors='replace')[:2000]
            raise PostHogApiError(f'{method} {url} returned HTTP {failure.code}: {detail}') from failure
        return json.loads(text) if text else None

    def project_url(self, path, query=None):
        url = f'{self.host}{self.project_path}/{path}'
        return f'{url}?{urllib.parse.urlencode(query)}' if query else url

    def list_all(self, path, query):
        url = self.project_url(path, query)
        results = []
        while url:
            page = self.request('GET', url)
            results.extend(page['results'])
            url = page['next']
        return results
