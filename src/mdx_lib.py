import copy
import re
import json
import logging
import urllib
import inspect
import requests
import time

DEFAULT_MDX_ENDPOINT = "https://oprpl.mdx.jp"

logger = logging.getLogger(__name__)


class MdxRestException(Exception):
    def __init__(self, message, status_code=0):
        self.message = message
        self.status_code = status_code


class MdxLib(object):
    """
    mdxのREST APIに対応したpythonライブラリ

    通常プロジェクトをサポートする。
    """

    def __init__(self, endpoint=DEFAULT_MDX_ENDPOINT, init_token=None):
        self._endpoint = endpoint
        self._token = init_token

    def _call_api(
        self, api, method="GET", data=None, with_token=True, refresh_token=True
    ):
        """
        内部のtokenをリフレッシュする
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if with_token:
            if self._token is None:
                raise MdxRestException("mdxlib: token is not specified")
            headers["Authorization"] = "JWT %s" % self._token
        url = urllib.parse.urljoin(self._endpoint, api)
        try:
            if method == "GET":
                res = requests.get(url, params=data, headers=headers)
            elif method == "POST":
                res = requests.post(url, data=json.dumps(data), headers=headers)
            elif method == "PUT":
                res = requests.put(url, data, headers=headers)
            elif method == "DELETE":
                res = requests.delete(url, headers=headers)
            return res
        finally:
            if refresh_token:
                self._refresh_token()

    def _login(self, auth_info):
        # 運用時は不要になる. ポータルに置き換わる.
        res = self._call_api(
            "/api/login/",
            method="POST",
            data=auth_info,
            with_token=False,
            refresh_token=False,
        )
        if res.status_code != 200:
            raise MdxRestException(
                "mdxlib: login is failed: {}".format(res.text), res.status_code
            )
        # token is body itself
        self._token = res.json()["token"]

    def _refresh_token(self):
        # refresh token
        data = {"token": self._token}
        res = self._call_api(
            "/api/refresh/", method="POST", data=data, refresh_token=False
        )
        if res.status_code != 200:
            raise MdxRestException("mdxlib: token refresh failed")
        resp_body = res.json()
        self._token = resp_body["token"]

    def _get_vm_info_by_name(self, project_id, vm_name, wait=True):

        for _ in range(10):
            vm_list = self.get_vm_list(project_id)

            for vm_info in vm_list['results']:
                if vm_info['name'] == vm_name:
                    return vm_info
            else:
                if wait is False:
                    return
                time.sleep(10)

    def _predict_vmnames(self, s):
        match = re.fullmatch(r"(.*)\[(\d+)-(\d+)\](.*)", s)
        if not match:
            single_range = re.fullmatch(r"(.*)\[(\d+)\](.*)", s)
            if single_range:
                return [s.replace('[', '').replace(']', '')]
            else:
                return [s]

        prefix, start, end, suffix = match.groups()
        width = max(len(start), len(end))
        return [f"{prefix}{str(i).zfill(width)}{suffix}" for i in range(int(start), int(end) + 1)]

    def _get_task_info(self, project_id, vm_name, task_id):

        vm_info = None
        for _ in range(10):
            vm_info = self._get_vm_info_by_name(project_id, vm_name)
            if vm_info is not None:
                break
            time.sleep(10)
        vm_histories = self.get_vm_history(vm_info['uuid'])
        for vm_history in vm_histories['results']:
            if vm_history['uuid'] == task_id:
                return vm_history
            else:
                return None

    def refresh_token(self):
        self._refresh_token()

    def login(self, auth_info):
        """
        mdx REST APIデバッグ時のトークンの取得。
        運用時はこの関数の代わりに、mdxのポータルサイトからトークンを取得すること。

        :params auth_info: 以下のような認証情報

        .. code-block:: json

          {
            "username": ユーザ名
            "password": REST APIを使用するためのパスワード
          }

        """
        self._login(auth_info)

    def deploy_vm(self, mdx_vm_spec) -> list:
        """
        VMを作成(デプロイ)する。(非同期)
        デプロイの完了を vm_info APIで確認すること。

        :param mdx_vm_spec: mdxでのVMのスペック情報

        mdxには「通常プロジェクト」と「専有プロジェクト」がある。通常プロジェクトの場合、
        ``mdx_spec`` で以下の項目を指定すること。

        .. code-block:: json

           {
              "catalog": カタログID
              "pack_num": パック数
              "pack_type": パックタイプ "gpu" または "cpu"
              "disk_size": 仮想ディスクサイズ(GB)
              "network_adapters":
              "os_type": OSタイプ "Linux" または "Windows"
              "power_on": デプロイ後起動するか否か (boolean)
              "project": プロジェクトID
              "shared_key": 公開鍵の内容
              "storage_network": ストレージネットワーク "sr-iov" または "pvrdma" または "portgroup"
              "template_name": vCenter上の仮想マシンテンプレート名
              "vm_name": 仮想マシン名
           }

        """
        # 専有プロジェクト
        # `cpu`: CPU数
        # `gpu`: GPU数
        # `memory`: メモリ(GB)

        # プロジェクトタイプにより設定する項目が異なる
        # 通常プロジェクト: pack数を指定する(API仕様書に書いてない?)
        # 専有型プロジェクト: cpu, memory数を指定する
        data = mdx_vm_spec
        res = self._call_api("/api/vm/deploy/", method="POST", data=data)
        if res.status_code != 202:
            raise MdxRestException(
                "mdxlib: deploy vm is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        # task id が返る
        logger.debug("deploy vm: {}".format(res.text))
        task_ids = res.json()['task_id']
        vm_names = self._predict_vmnames(mdx_vm_spec['vm_name'])
        tasks = []
        for vm_name in vm_names:
            for i, task_id in enumerate(task_ids):
                task_info = self._get_task_info(mdx_vm_spec['project'], vm_name, task_id)
                if task_info is not None:
                    tasks.append(task_info)
                    del task_ids[i]
                    break
        return tasks

    def clone_vm(self, original_vm_id: str, mdx_vm_spec: dict):
        """
        VMをクローンする。

        :param original_vm_id: クローン元の仮想マシンID
        :param mdx_vm_spec: mdxでのVMのスペック情報

        .. code-block:: json

           {
              "project": プロジェクトID
              "vm_name": 仮想マシン名
              "storage_network": ストレージネットワーク "sr-iov" または "pvrdma" または "portgroup"
              "pack_type": パックタイプ "gpu" または "cpu"
              "pack_num": パック数
              "service_level": サービスレベルの指定 "spot" または "guarantee"
              "network_adapters": ネットワークアダプタ情報(list)
           }

        """

        data = copy.deepcopy(mdx_vm_spec)
        res = self._call_api(f"/api/vm/{original_vm_id}/clone/", method="POST", data=data)
        func_name = inspect.currentframe().f_code.co_name
        if res.status_code != 202:
            raise MdxRestException(
                "mdxlib: {} is failed: {}".format(func_name, res.text),
                status_code=res.status_code,
            )

        logger.debug("{}: {}".format(func_name, res.text))
        resp_body = res.json()
        return resp_body["task_id"]

    def destroy_vm(self, vm_id):
        """
        仮想マシンを削除する。(非同期)
        仮想マシンの削除の完了を vm_info APIで確認すること。
        呼び出し側で予めVMをシャットダウンしておくこと。
        """
        res = self._call_api("/api/vm/{}/destroy/".format(vm_id), method="POST")
        if res.status_code != 202:
            raise MdxRestException(
                "mdxlib: destroy vm is failed: {}".format(res.text),
                status_code=res.status_code,
            )

        resp_body = res.json()
        return resp_body["task_id"]

    def shutdown_vm(self, vm_id):
        """
        仮想マシンのゲストOSのシャットダウン(非同期)
        仮想マシンのシャットダウンの完了を vm_info APIで確認すること。
        """
        res = self._call_api("/api/vm/{}/shutdown/".format(vm_id), method="POST")
        if res.status_code != 202:
            raise MdxRestException(
                "mdxlib: token refresh is failed: {}".format(res.text),
                status_code=res.status_code,
            )

        return res.json()

    def power_off_vm(self, vm_id):
        """
        仮想マシンの強制停止 (非同期)
        """
        #
        res = self._call_api("/api/vm/{}/power_off/".format(vm_id), method="POST")
        if res.status_code != 202:
            raise MdxRestException(
                "mdxlib: power_off vm is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        return res.json()

    def power_on_vm(self, vm_id, service_level="spot"):
        """
        仮想マシンの起動 (電源オン) (非同期)
        """
        data = {'service_level': service_level}
        res = self._call_api("/api/vm/{}/power_on/".format(vm_id), method="POST", data=data)
        if res.status_code != 202:
            raise MdxRestException(
                "mdxlib: power_on vm is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        return res.json()

    def reboot_vm(self, vm_id):
        """
        仮想マシンの再起動 (非同期)
        """
        res = self._call_api("/api/vm/{}/reboot/".format(vm_id), method="POST")
        if res.status_code != 202:
            raise MdxRestException(
                "mdxlib: reboot vm is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        return res.json()

    # その他
    def get_assigned_projects(self):
        res = self._call_api("/api/project/assigned/", method="GET")
        if res.status_code != 200:
            raise MdxRestException(
                "mdxlib: get project is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        return res.json()

    def get_project_history(self, project_id, page=1, page_size=10000):
        data = dict(
            page=page,
            page_size=page_size,
        )
        res = self._call_api(
            "/api/history/project/{}/".format(project_id), data=data, method="GET"
        )
        if res.status_code != 200:
            raise MdxRestException(
                "mdxlib: get project history is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        return res.json()

    def get_vm_list(self, project_id, page=1, page_size=10000):
        data = dict(
            page=page,
            page_size=page_size,
        )
        res = self._call_api(
            "/api/vm/project/{}/".format(project_id), data=data, method="GET"
        )
        if res.status_code != 200:
            raise MdxRestException(
                "mdxlib: get project history is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        return res.json()

    def get_vm_history(self, vm_id, page=1, page_size=1000):
        res = self._call_api(
            "/api/history/vm/{}/?page={}&page_size={}".format(vm_id, page, page_size),
            method="GET")
        if res.status_code != 200:
            raise MdxRestException(
                "mdxlib: get vm history is failed: {} {}".format(vm_id, res.text),
                status_code=res.status_code,
            )
        return res.json()

    def get_vm_info(self, vm_id):
        """
        vm_idで指定した仮想マシンの詳細情報を取得する

        :param vm_id:  仮想マシンID
        :return: VM情報

        .. code-block:: json

          {
             `name`: 仮想マシン名
             `vm_id`: 仮想マシンID
             `os_type`:
             `status`: 仮想マシンの状態 "PowerON" "PowerOFF" "Suspended" "NotFound" "Deploying" "Detached"
             `vmware_tools`:
             `cpu`:
             `memory`:
             `gpu`:
             `service_networks`:
             `storage_network`:
             `hard_disks`:
             `dvd_media`:
             `vcenter`:
             `esxi`:
             `pack_type`:
             `pack_num`:
          }
        ```
        """
        res = self._call_api("/api/vm/{}/".format(vm_id), method="GET")
        if res.status_code != 200:
            raise MdxRestException(
                "mdxlib: get vm info is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        result = res.json()
        result["vm_id"] = vm_id
        return result

    def get_vm_catalogs(self, project_id):
        res = self._call_api(
            "/api/catalog/project/{}/?page=1&page_size=10000".format(project_id),
            method="GET",
        )
        if res.status_code != 200:
            raise MdxRestException(
                "proj-{}|mdxlib: get_vm_catalog is failed: {}".format(
                    project_id, res.text
                ),
                status_code=res.status_code,
            )
        return res.json()

    def get_allow_acl_ipv4_info(self, segment_id):
        res = self._call_api("/api/acl/segment/{}/?page=1&page_size=10000".format(segment_id), method="GET")
        if res.status_code != 200:
            raise MdxRestException(
                "mdxlib: get_allow_acl_ipv4_info is failed: {} {}".format(
                    segment_id, res.text
                ),
                status_code=res.status_code,
            )
        return res.json()

    def add_allow_acl_ipv4_info(self, allow_acl_spec):
        # TODO: validate allow_acl_spec with jsonschema
        res = self._call_api("/api/acl/", data=allow_acl_spec, method="POST")
        if res.status_code != 201:
            raise MdxRestException(
                "mdxlib: add_allow_acl_ipv4_info is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        return res.json()

    def edit_allow_acl_ipv4_info(self, acl_ipv4_id, allow_acl_spec):
        # TODO: validate allow_acl_spec with jsonschema
        res = self._call_api("/api/acl/{}/".format(acl_ipv4_id),
                             data=json.dumps(allow_acl_spec), method="PUT")
        if res.status_code != 200:
            raise MdxRestException(
                "mdxlib: edit_allow_acl_ipv4_info is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        return res.json()

    def delete_allow_acl_ipv4_info(self, acl_ipv4_id):
        res = self._call_api("/api/acl/{}/".format(acl_ipv4_id), method="DELETE")
        if res.status_code != 204:
            raise MdxRestException(
                "mdxlib: delete_allow_acl_ipv4_info is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        # 返り値なし

    def get_allow_acl_ipv6_info(self, segment_id):
        res = self._call_api(
            "/api/acl_v6/segment/{}/?page=1&page_size=10000".format(segment_id),
            method="GET")
        if res.status_code != 200:
            raise MdxRestException(
                "mdxlib: get_allow_acl_ipv6_info is failed: {} {}".format(
                    segment_id, res.text
                ),
                status_code=res.status_code,
            )
        return res.json()

    def add_allow_acl_ipv6_info(self, allow_acl_spec):
        # TODO: validate allow_acl_spec with jsonschema
        res = self._call_api("/api/acl_v6/", data=allow_acl_spec, method="POST")
        if res.status_code != 201:
            raise MdxRestException(
                "mdxlib: add_allow_acl_ipv6_info is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        return res.json()

    def edit_allow_acl_ipv6_info(self, acl_ipv6_id, allow_acl_spec):
        # TODO: validate allow_acl_spec with jsonschema
        res = self._call_api("/api/acl_v6/{}/".format(acl_ipv6_id),
                             data=json.dumps(allow_acl_spec), method="PUT")
        if res.status_code != 200:
            raise MdxRestException(
                "mdxlib: edit_allow_acl_ipv6_info is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        return res.json()

    def delete_allow_acl_ipv6_info(self, acl_ipv6_id):
        res = self._call_api("/api/acl_v6/{}/".format(acl_ipv6_id), method="DELETE")
        if res.status_code != 204:
            raise MdxRestException(
                "mdxlib: delete_allow_acl_ipv6_info is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        # 返り値なし

    def get_segments(self, project_id):
        res = self._call_api(
            "/api/segment/project/{}/all/".format(project_id), method="GET"
        )
        if res.status_code != 200:
            raise MdxRestException(
                "mdxlib: get_segments is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        return res.json()

    def get_segment_summary(self, project_id, segment_id):
        res = self._call_api("/api/segment/{}/summary".format(segment_id), method="GET")
        if res.status_code != 200:
            raise MdxRestException(
                "proj-{}:segment-{}|mdxlib: get_segment_summary is failed: {}".format(
                    project_id, segment_id, res.text
                ),
                status_code=res.status_code,
            )
        return res.json()

    # dnat
    def get_dnat(self, project_id, page=1, page_size=10000):
        data = dict(
            page=page,
            page_size=page_size,
        )
        res = self._call_api("/api/dnat/project/{}".format(project_id), data=data, method="GET")
        if res.status_code != 200:
            raise MdxRestException(
                "proj-{}|mdxlib: get_dnat is failed: {}".format(
                    project_id, res.text
                ),
                status_code=res.status_code,
            )
        return res.json()

    def add_dnat(self, project_id, nat_spec):
        res = self._call_api("/api/dnat/", data=nat_spec, method="POST")
        if res.status_code != 201:
            raise MdxRestException(
                "proj-{}:segment-{}|mdxlib: add_dnat is failed: {}".format(
                    project_id, nat_spec["segment"], res.text
                ),
                status_code=res.status_code,
            )
        return res.json()

    def edit_dnat(self, project_id, dnat_id, nat_spec):
        res = self._call_api("/api/dnat/{}/".format(dnat_id), data=json.dumps(nat_spec), method="PUT")
        if res.status_code != 200:
            raise MdxRestException(
                "proj-{}:segment-{}|mdxlib: edit_dnat is failed: {}".format(
                    project_id, nat_spec["segment"], res.text
                ),
                status_code=res.status_code,
            )
        return res.json()

    def delete_dnat(self, project_id, dnat_id):
        res = self._call_api("/api/dnat/{}/".format(dnat_id), method="DELETE")
        if res.status_code != 204:
            raise MdxRestException(
                "proj-{}:dnat-{}|mdxlib: delete_dnat is failed: {}".format(
                    project_id, dnat_id, res.text
                ),
                status_code=res.status_code,
            )
        # 返り値(json)なし
        return res

    def get_assignable_global_ipv4(self, project_id):
        res = self._call_api(
            "/api/global_ip/project/{}/assignable/".format(project_id),
            method="GET"
        )
        if res.status_code != 200:
            raise MdxRestException(
                "mdxlib: get project history is failed: {}".format(res.text),
                status_code=res.status_code,
            )
        return res.json()
