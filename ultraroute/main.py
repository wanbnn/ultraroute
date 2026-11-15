import os
import uvicorn


def main():
    uvicorn.run('ultraroute.api:create_app', factory=True, host='127.0.0.1',
                port=int(os.getenv('ULTRAROUTE_PORT', '8989')), workers=1, access_log=False)


if __name__ == '__main__':
    main()
