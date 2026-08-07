# Contributing to wechat-decrypt

Thanks for your interest! This project aims to be the most comprehensive WeChat 4.0 database decryption tool on Windows.

## How to Contribute

### Bug Reports
Open an issue with:
- Windows version
- WeChat version (Settings → About)
- Error message / stack trace
- Steps to reproduce

### Feature Requests
Open an issue tagged `enhancement`. Describe the use case.

### Pull Requests
1. Fork the repo
2. Create a feature branch (`git checkout -b feat/your-feature`)
3. Keep changes focused — one feature per PR
4. Test on your own WeChat instance first
5. Open PR against `main`

### Code Style
- Python 3.10+
- PEP 8 (ruff with default rules)
- Type hints preferred but not required
- Docstrings for public functions

## Development Setup

```bash
git clone https://github.com/328336690/wechat-decrypt.git
cd wechat-decrypt
pip install -r requirements.txt
```

> ⚠️ Requires WeChat 4.0 running and admin privileges for memory scanning.

## License
MIT — see [LICENSE](LICENSE).
