# 기여 안내 (Contributing)

T6 Viewer에 관심 가져 주셔서 고맙습니다. 버그 제보, 기능 제안, 문서 수정, 코드 PR 모두 환영합니다.
English issues and pull requests are welcome too.

## 먼저 지켜 주세요

- **개인 영상·토큰·위치 정보를 올리지 마세요.** 이슈, PR, 테스트 데이터 모두 해당합니다. Bearer 토큰은 계정 권한입니다.
  재현에 영상이 꼭 필요하면 번호판·얼굴·위치를 가린 짧은 클립인지 확인하고 올려 주세요.
- 보안 취약점은 공개 이슈 대신 [SECURITY.md](SECURITY.md)의 방법으로 알려 주세요.
- 모든 참여자는 [행동 강령](CODE_OF_CONDUCT.md)을 따릅니다.

## 라이선스 (inbound = outbound)

이 프로젝트는 [AGPL-3.0-or-later](LICENSE)입니다. PR을 보내면 그 기여분도 같은 라이선스로 배포되는 것에 동의하는 것으로 봅니다.
다른 라이선스의 코드를 가져올 때는 AGPL과 호환되는지 확인하고, 출처와 라이선스를 PR에 적은 뒤
[licenses/THIRD_PARTY_NOTICES.md](licenses/THIRD_PARTY_NOTICES.md)에 추가해 주세요.

## 개발 환경

```powershell
git clone https://github.com/zyongho/T6_Viwer.git
cd T6_Viwer
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt pytest
python run.py
```

## 테스트

```powershell
python -m pytest -q
```

- 테스트는 실제 Tesla 파일이나 토큰 없이 돌아갑니다(합성 영상·합성 암호화 파일 사용).
- 테스트는 임시 폴더를 쓰므로 사용자의 실제 설정(`%LOCALAPPDATA%\T6Viewer`)을 건드리지 않습니다. 새 테스트도 이 원칙을 지켜 주세요.
- 동작을 바꾸거나 버그를 고칠 때는 그 내용을 확인하는 테스트를 함께 넣어 주세요.

## PR 보내기

1. 큰 변경은 먼저 이슈로 방향을 맞춰 주세요.
2. `main`에서 브랜치를 만들어 작업하고, 한 PR에는 한 가지 주제만 담아 주세요.
3. 주변 코드의 스타일(이름, 주석 양, 구조)을 따라 주세요.
4. 화면 문구는 한국어로, 짧고 쉬운 말로 써 주세요.
5. 사용자에게 보이는 변경은 [docs/CHANGELOG.md](docs/CHANGELOG.md)와 필요하면 [docs/help.html](docs/help.html)에도 반영해 주세요.
6. `python -m pytest -q`가 통과하는지 확인해 주세요.
