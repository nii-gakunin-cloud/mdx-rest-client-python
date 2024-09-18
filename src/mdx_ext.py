#
# mdx extension
#
import json
import jsonschema
import logging
import re
import sys
import time
from mdx_lib import MdxLib, MdxRestException, DEFAULT_MDX_ENDPOINT
SLEEP_TIME_SEC = 5
SLEEP_COUNT = 120
DEPLOY_VM_SLEEP_COUNT = 240
DELETABLE_STATE = ["PowerOFF", "Deallocated"]

logger = logging.getLogger(__name__)
# project_id, vm_name, os_typeを外した
# 通常プロジェクト
MDX_VM_SPEC_SCHEMA = {
    "additionalProperties": False,
    "type": "object",
    "properties": {
        "catalog": {
            "type": "string"
        },
        "template_name": {
            "type": "string"
        },
        "pack_num": {
            "type": "integer"
        },
        "pack_type": {
            "type": "string",
            "enum": [
                "cpu",
                "gpu"
            ]
        },
        "gpu": {
            "type": "string"
        },
        "disk_size": {
            "type": "integer"  # GB
        },
        "network_adapters": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "adapter_number": {
                        "type": "integer"
                    },
                    "segment": {
                        "type": "string"
                    }
                }
            }
        },
        "shared_key": {
            "type": "string"
        },
        "storage_network": {
            "type": "string",
            "enum": [
                "sr-iov",
                "pvrdma",
                "portgroup",
            ],
        },
        "service_level": {
            "type": "string"
        }
    }
}


class MdxResourceExt(object):
    """
    mdx REST API にアクセスするためのPythonクライアントライブラリ。

    mdx REST API による仮想マシンの作成、状態取得、ネットワーク設定などの機能を提供する。

    :param init_token: mdx ユーザポータルから取得した mdx REST API 認証トークン
    :param endpoint: mdx REST API エンドポイント URL (オプショナル)
    """
    # initの説明

    def __init__(self, init_token=None, endpoint=DEFAULT_MDX_ENDPOINT):
        self._mdxlib = MdxLib(endpoint=endpoint, init_token=init_token)
        self._project_id = None

    def _check_project_id(self):
        if self._project_id is None:
            raise MdxRestException("call set_project_id to set target mdx project")

    def login(self, auth_info):
        """
        mdx REST API 認証トークンを発行する（開発用）
        通常利用時は mdx ユーザポータルから認証トークンを取得し、__init__ の init_token 引数に指定すること。

        :param auth_info: 以下のような、mdx REST APIの認証情報

        .. code-block:: json

          {
             "username": mdx REST API ユーザ名
             "password": mdx REST API パスワード
          }

        """
        self._mdxlib.login(auth_info)

    def refresh_token(self):
        """
        mdx REST API 認証トークンを更新する
        """
        self._mdxlib.refresh_token()

    def deploy_vm(self, vm_name, vm_spec, wait_for=True):
        """
        仮想マシンのデプロイを実行する。wait_forが ``True`` の場合、仮想マシンにIPv4アドレスが付与されるまで待つ。

        :param vm_name: 仮想マシン名
        :param vm_spec: 仮想マシンの仕様（ハードウェアのカスタマイズ項目）

        .. code-block:: json

          {
            "catalog": カタログID
            "disk_size": 仮想ディスクサイズ(GB)
            "gpu": GPU数(数値を文字列で指定)
            "pack_type": パックタイプ(※通常プロジェクトの場合に指定) "gpu" または "cpu" を指定
            "pack_num": パック数(※通常プロジェクトの場合に指定)
            "network_adapters": [
               {
                  "adapter_number": ネットワーク番号
                  "segment": ネットワークセグメントID
               }
            ],
            "shared_key": 仮想マシンへのSSH接続用公開鍵の文字列
            "storage_network": ストレージネットワーク "sr-iov", "pvrdma", "portgroup" のいずれかを指定
            "template_name": vCenter上の仮想マシンテンプレート名
          }

        :param wait_for: 仮想マシンにIPv4アドレスが付与されるまで待つ場合 ``True`` を指定
        :returns: 仮想マシン情報。詳細は get_vm_info() を参照のこと。
        """
        # 専有プロジェクトの場合
        # "cpu": CPU数(※専有プロジェクトの場合に必要)
        # "memory": メモリ量(GB) (※専有プロジェクトの場合に必要)

        self._check_project_id()

        jsonschema.validate(vm_spec, MDX_VM_SPEC_SCHEMA)

        # OSタイプ、デプロイ後の起動指定は固定値とする
        vm_spec["os_type"] = "Linux"
        vm_spec["power_on"] = True

        vm_spec["project"] = self._project_id
        vm_spec["vm_name"] = vm_name

        self._mdxlib.deploy_vm(vm_spec)
        vm_id = self._find_vm(vm_name)
        if wait_for:
            # historyで待つ
            # for _i in range(0, SLEEP_COUNT):
            #     # vm_info = self._mdxlib.get_vm_info(vm_id)
            #     vm_history = self._mdxlib.get_vm_history(vm_id)
            #     # print(json.dumps(vm_history["results"][0], indent=2))
            #     #
            #     if vm_history["results"][0]["status"] == "Completed":
            #         break
            #     time.sleep(SLEEP_TIME_SEC)
            # TODO: sshできるまで待つ?
            self._wait_until(vm_id, "PowerON")
            for i in range(0, DEPLOY_VM_SLEEP_COUNT):
                vm_info = self._mdxlib.get_vm_info(vm_id)
                private_ip_address = vm_info["service_networks"][0]["ipv4_address"][0]
                logger.debug("{} {}".format(i, private_ip_address))
                if re.match(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$", private_ip_address) is not None:
                    break
                time.sleep(SLEEP_TIME_SEC)
            else:
                raise MdxRestException("{}: timeout: allocate ip address".format(vm_name))

        return self._mdxlib.get_vm_info(vm_id)

    def destroy_vm(self, vm_name, wait_for=True):
        """
        仮想マシンの削除を実行する。事前に仮想マシンを PowerOFF 状態にしておく必要がある。

        :param vm_name: 仮想マシン名
        :param wait_for: 削除完了を待つ場合 ``True`` を指定
        """

        self._check_project_id()
        vm_id = self._find_vm(vm_name)

        vm_info = self._get_vm_info_by_id(vm_id)
        if vm_info["status"] not in DELETABLE_STATE:
            raise MdxRestException(
                "mdxext: destroy_vm vm status is not PowerOFF or Deallocated but {}, please power_off first".format(vm_info["status"]))

        # TODO: 仮想マシンの事前状態のチェックがmdx rest api側にない?
        self._mdxlib.destroy_vm(vm_id)

        for _i in range(0, SLEEP_COUNT):
            # 仮想マシン情報から消えるまで待つ
            time.sleep(SLEEP_TIME_SEC)
            res = self._mdxlib._call_api("/api/vm/{}/".format(vm_id), method="GET")
            logger.debug("{}: destroy_vm status_code {} {}".format(vm_name, res.status_code, res.text))
            if res.status_code == 404:
                # 削除完了
                break
        else:
            raise MdxRestException("destroy_vm is failed: {}".format(vm_name))

    def power_on_vm(self, vm_name, service_level="spot", wait_for=True):
        """
        仮想マシンの起動 (PowerON) を実行する。

        :param vm_name: 仮想マシン名
        :param wait_for: 起動の完了を待つ場合 ``True`` を指定
        """
        self._check_project_id()
        vm_id = self._find_vm(vm_name)

        vm_info = self._get_vm_info_by_id(vm_id)
        if vm_info["status"] == "PowerON":
            logger.debug("vm is already power on")
            return
        if vm_info["status"] not in DELETABLE_STATE:
            raise MdxRestException(
                "mdxext: power_off_vm vm status is not PowerOFF or deallocated but {}".format(
                    vm_info["status"]))
        self._mdxlib.power_on_vm(vm_id, service_level)
        if wait_for:
            self._wait_until(vm_id, "PowerON")

    def power_off_vm(self, vm_name, wait_for=True):
        """
        仮想マシンの強制停止 (PowerOFF) を実行する。

        :param vm_name: 仮想マシン名
        :param wait_for: 強制停止の完了を待つ場合 ``True`` を指定
        """
        self._check_project_id()
        vm_id = self._find_vm(vm_name)

        vm_info = self._get_vm_info_by_id(vm_id)
        if vm_info["status"] in DELETABLE_STATE:
            logger.debug("vm is already power off or deallocated")
            return
        if vm_info["status"] != "PowerON":
            raise MdxRestException("mdxext: power_off_vm vm status is not PowerON but {}".format(vm_info["status"]))

        self._mdxlib.power_off_vm(vm_id)
        if wait_for:
            self._wait_until(vm_id, "PowerOFF")

    def power_shutdown_vm(self, vm_name, wait_for=True):
        """
        仮想マシンのゲストOSのシャットダウンを実行する。

        :param vm_name: 仮想マシン名
        :param wait_for: シャットダウンの完了を待つ場合 ``True`` を指定
        """
        self._check_project_id()
        vm_id = self._find_vm(vm_name)

        vm_info = self._get_vm_info_by_id(vm_id)
        # 事前条件　PowerOn
        if vm_info["status"] in DELETABLE_STATE:
            logger.debug("vm is already power off or deallocated")
            return
        if vm_info["status"] != "PowerON":
            raise MdxRestException("mdxext: power_shutdown_vm vm status is not PowerON but {}".format(vm_info["status"]))
        # TODO: 操作履歴IDを返す?
        self._mdxlib.shutdown_vm(vm_id)

        if wait_for:
            self._wait_until(vm_id, "PowerOFF")

    def reboot_vm(self, vm_name, wait_for=True):
        """
        仮想マシンの再起動を実行する。

        :param vm_name: 仮想マシン名
        :param wait_for: 再起動の完了を待つ場合 ``True`` を指定
        """
        # 実行履歴で確認したところ10秒で完了する
        self._check_project_id()
        vm_id = self._find_vm(vm_name)

        vm_info = self._get_vm_info_by_id(vm_id)
        # 事前条件　PowerOn
        if vm_info["status"] != "PowerON":
            raise MdxRestException("mdxext: reboot_vm vm status is not PowerON but {}".format(vm_info["status"]))
        self._mdxlib.reboot_vm(vm_id)

        if wait_for:
            self._wait_until(vm_id, "PowerON")

    def _get_vm_info_by_id(self, vm_id):
        return self._mdxlib.get_vm_info(vm_id)

    def get_vm_info(self, vm_name):
        """
        仮想マシンの詳細情報を取得する

        :param vm_name: 仮想マシン名
        :returns: 以下のような仮想マシン情報

        .. code-block:: json

          {
            "name": 仮想マシン名
            "vm_id": 仮想マシンID
            "os_type": OSタイプ
            "status": 仮想マシンの状態 "PowerON" "PowerOFF" "Deallocated" "Suspended" "NotFound" "Deploying" "Detached" のいずれか
            "vmware_tools": [
               {
                 "status": VMware Tools状態
                 "version": VMware Toolsバージョン
               }
            ],
            "cpu": CPU数
            "memory": メモリ量の文字列表現 (例: "2 GB")
            "gpu": GPU数の文字列表現 (例: "1")
            "service_networks": [
              {
                "adapter_number": ネットワーク番号
                "ipv4_address": IPv4アドレスのリスト
                "ipv6_address": IPv6アドレスのリスト
                "segment": ネットワークセグメント名
              }
            ],
            "storage_networks": [
              {
                "adapter_number": ネットワーク番号
                "ipv4_address": IPv4アドレスのリスト
                "ipv6_address": IPv6アドレスのリスト
                "type": ネットワークタイプ "sr-iov", "pvrdma", "portgroup" のいずれか
              }
            ],
            "hard_disks": [
              {
                "disk_number": 仮想ディスク番号
                "device_key": 仮想ディスクデバイスキー
                "capacity": 仮想ディスクサイズ
                "datastore": データストア名
              }
            ],
            "dvd_media": ゲストOSがマウントしているISOイメージ
            "vcenter": vCenter名
            "esxi": ESXi名
            "pack_type": パックタイプ "cpu", "gpu" のいずれか
            "pack_num": パック数
          }

        """
        self._check_project_id()
        vm_id = self._get_vm_id_by_vm_name(vm_name)
        if vm_id is None:
            return None
        return self._get_vm_info_by_id(vm_id)

    def get_vm_list(self):
        """
        プロジェクトに属する仮想マシン情報を取得する

        :returns: 以下のような、仮想マシン情報のリスト

        .. code-block:: json

          [
            {
              "uuid": 仮想マシンID
              "name": 仮想マシン名
              "status": 仮想マシン状態 "PowerON" "PowerOFF" "Deallocated" "Suspended" "NotFound" "Deploying" "Detached" のいずれか
              "vcenter": vCenter名
              "running_tasks": 実行中のタスクのリスト
            }
          ]

        """
        self._check_project_id()
        return list(self.vm_info_iter())

    def get_vm_catalogs(self):
        """
        プロジェクトに紐づいた仮想マシンデプロイカタログ情報を取得する

        :returns: 以下のような、カタログのリスト

        .. code-block:: json

          [
            {
              "uuid": カタログID
              "name": カタログ名
              "type": カタログのタイプ
              "template_name": vCenter上の仮想マシンテンプレート名
              "os_type": OS種別 "Linux", "Windows" のいずれか
              "os_name": OS名 (例: "CentOS")
              "os_version": OSバージョン
              "hw_version": ハードウェアバージョン
              "description": 説明
              "login_username": OSログインユーザ名
            }
          ]

        """
        self._check_project_id()
        return self._mdxlib.get_vm_catalogs(self._project_id)

    def get_assigned_projects(self):
        """
        ユーザに紐付いたプロジェクト情報を取得する

        :returns: 以下のような、プロジェクト情報のリスト

        .. code-block:: json

          [
            {
              "uuid": 機関ID
              "name": 機関名
              "projects": [
                 {
                    "uuid": プロジェクトID
                    "name": プロジェクト名
                    "type": プロジェクトのタイプ "専有" "通常" のいずれか
                    "expired": プロジェクトの期限が切れたか否か (boolean)
                 }
              ]
            }
          ]

        """
        # _check_project_idは不要
        return self._mdxlib.get_assigned_projects()

    def set_current_project_id(self, project_id):
        """
        操作対象のmdxのプロジェクトIDを設定する
        """
        self._project_id = project_id

    def set_current_project_by_name(self, project_name):
        """
        操作対象のmdxのプロジェクトをプロジェクト名で設定する
        """
        org_list = self._mdxlib.get_assigned_projects()
        for org in org_list:
            for proj in org["projects"]:
                if proj["name"] == project_name:
                    self._project_id = proj["uuid"]
                    return
        else:
            # 見つからない場合
            raise MdxRestException("mdx_ext: project {} is not found".format(project_name))

    def get_current_project(self):
        """
        操作対象のmdxのプロジェクトの取得

        :returns: 以下のような、プロジェクトに属するAllow ACL IPv4の情報のリスト

        .. code-block:: json

          {
             "uuid": プロジェクトID
             "name": プロジェクト名
             "type": プロジェクトのタイプ "専有" "通常" のいずれか
             "expired": プロジェクトの期限が切れたか否か (boolean)
          }

        """
        if self._project_id is None:
            return None
        org_list = self._mdxlib.get_assigned_projects()
        for org in org_list:
            for proj in org["projects"]:
                if proj["uuid"] == self._project_id:
                    return proj
        else:
            # 見つからない場合
            return None

    # network
    def get_allow_acl_ipv4_info(self, segment_id):
        """
        Allow ACL IPv4情報の取得

        :param segment_id: ネットワークセグメントID
        :returns: 以下のような、プロジェクトに属するAllow ACL IPv4の情報のリスト

        .. code-block:: json

          [
            {
              "uuid": Allo ACL IPv4 ID
              "src_address": Srcアドレス
              "src_mask":  Srcマスク (string で指定　例: "24")
              "src_port": Srcポート (string)
              "dst_address": Dstアドレス
              "dst_mask": Dstマスク(string で指定　例: "24")
              "dst_port": Dstポート (string)
              "protocol": プロトコル "ICMP" "TCP" "UDP" のいずれか
            }
          ]

        """
        self._check_project_id()
        return self._mdxlib.get_allow_acl_ipv4_info(segment_id)

    def add_allow_acl_ipv4_info(self, allow_acl_spec):
        """
        指定したセグメントにAllow ACL IPv4を追加する

        :param allow_acl_spec: 以下のような、追加するAllow ACL IPv4の仕様

        .. code-block:: json

          {
            "segment": ネットワークセグメントID
            "src_address": Src IPv4アドレス
            "src_mask": Srcマスク
            "src_port": Srcポート
            "dst_address": Dst IPv4アドレス
            "dst_mask": Dstマスクの文字列表現
            "dst_port": Dstポートの文字列表現
            "protocol": プロトコル "ICMP" "TCP" "UDP" のいずれか
          }

        """
        self._check_project_id()
        return self._mdxlib.add_allow_acl_ipv4_info(allow_acl_spec)

    def delete_allow_acl_ipv4_info(self, acl_ipv4_id):
        """
        :param acl_ipv4_id: 削除対象の Allow ACL IPv4 ID
        """
        self._check_project_id()
        self._mdxlib.delete_allow_acl_ipv4_info(acl_ipv4_id)

    # project
    def get_project_history(self):
        """
        プロジェクト内における操作履歴の情報を取得する

        :returns: 以下のような、プロジェクト操作履歴情報のリスト

        .. code-block:: json

          [
            {
              "uuid": 操作履歴ID
              "project": プロジェクトID
              "user_name": 操作ユーザ名
              "type": 操作種別
              "object_uuid": 操作対象オブジェクトID
              "object_name": 操作対象オブジェクト名
              "start_datetime": 開始日付 (YYYY-mm-dd HH:MM:SS)
              "end_datetime": 終了日付 (YYYY-mm-dd HH:MM:SS)
              "status": ステータス "Running" "Completed" "Failed" のいずれか
              "progress": 進捗率 (%)
              "error_message": エラーメッセージ
              "error_detail": エラー詳細
            }
          ]
        """
        self._check_project_id()
        return list(self.project_history_iter())

    def vm_info_iter(self):
        """
        仮想マシン一覧をイテレータとして返す。
        """
        # TODO: 公開するか?決める
        self._check_project_id()
        current_page = 1
        page_size = 100
        # ページ番号で制御する(URLではなく)
        while True:
            vm_list = self._mdxlib.get_vm_list(self._project_id,
                                               page=current_page,
                                               page_size=page_size)
            for vm_info in vm_list["results"]:
                yield vm_info
            # VM情報のロックが必要? (ページをめくる間にVMが増減したらどうするか?)
            if vm_list["next"] is None:
                # StopIteration is RuntimeError
                return
            current_page += 1

    def project_history_iter(self):
        """
        プロジェクト操作履歴をイテレータとして返す。
        """
        self._check_project_id()
        current_page = 1
        page_size = 10000
        while True:
            history_list = self._mdxlib.get_project_history(self._project_id,
                                                            page=current_page,
                                                            page_size=page_size)
            for history in history_list["results"]:
                yield history

            if history_list["next"] is None:
                return
            current_page += 1

    def get_assignable_global_ipv4(self):
        self._check_project_id()
        return self._mdxlib.get_assignable_global_ipv4(self._project_id)

    def dnat_iter(self):
        self._check_project_id()
        current_page = 1
        page_size = 100
        while True:
            dnat_list = self._mdxlib.get_dnat(self._project_id, page=current_page, page_size=page_size)
            for dnat in dnat_list["results"]:
                yield dnat

            if dnat_list["next"] is None:
                return
            current_page += 1

    def get_segments(self):
        """
        プロジェクトに紐付いたネットワークセグメント情報を取得する。

        :returns: 以下のような、プロジェクトに紐付いたネットワークセグメント情報のリスト

        .. code-block:: json

          [
            {
              "uuid": ネットワークセグメントID
              "name": ネットワークセグメント名
              "default": プロジェクト作成時に作成されるデフォルトのネットワークセグメントか否か(boolean)
            }
          ]

        """
        self._check_project_id()
        return self._mdxlib.get_segments(self._project_id)

    def get_segment_summary(self, segment_id):
        """
        ネットワークセグメントのサマリ情報を取得する

        :param segment_id: ネットワークセグメントID
        :returns: 以下のような、ネットワークセグメントのサマリ情報

        .. code-block:: json

          {
            "vlan_id": VLAN ID
            "vni": VNI
            "ip_range": IPアドレス範囲
          }

        """
        self._check_project_id()
        return self._mdxlib.get_segment_summary(self._project_id, segment_id)

    def get_dnat(self):
        """
        プロジェクトに属するDNAT情報の取得

        :returns: 以下のような、プロジェクトに属する DNAT 情報のリスト

        .. code-block:: json

          [
            {
              "uuid": DNAT ID
              "pool_address": 転送元グローバルIPv4アドレス
              "segument": セグメント名
              "dst_address": 転送先プライベートIPアドレス
            }
          ]

        """
        return list(self.dnat_iter())

    def add_dnat(self, dnat_spec):
        """
        指定したセグメントにDNATを追加する

        :param dnat_spec: 以下のような DNAT 設定情報

        .. code-block:: json

          {
            "pool_address": 転送元グローバルIPv4アドレス。プロジェクトの未使用グローバルIPアドレスを指定する。
            "segment": ネットワークセグメントID
            "dst_address": 転送先プライベートIPアドレス。セグメントIPアドレス範囲内のIPアドレスを指定する。
          }

        """
        # TODO: validate
        return self._mdxlib.add_dnat(self._project_id, dnat_spec)

    def edit_dnat(self, dnat_id, dnat_spec):
        """
        指定したDNAT情報を更新する

        :param dnat_id: DNAT ID
        :param dnat_spec: 以下のような DNAT 設定情報

        .. code-block:: json

          {
            "pool_address": 転送元グローバルIPv4アドレス。プロジェクトの未使用グローバルIPアドレスを指定する。
            "segment": ネットワークセグメントID
            "dst_address": 転送先プライベートIPアドレス。セグメントIPアドレス範囲内のIPアドレスを指定する。
          }

        """
        # TODO: validate
        return self._mdxlib.edit_dnat(self._project_id, dnat_id, dnat_spec)

    def delete_dnat(self, dnat_id):
        """
        DNATの削除を実行する

        :param dnat_id: DNAT ID
        """
        self._mdxlib.delete_dnat(self._project_id, dnat_id)
        # 返り値なし

    def _get_vm_id_by_vm_name(self, vm_name):
        target_vm = None
        for vm in self.vm_info_iter():
            if vm["name"] == vm_name:
                target_vm = vm
                break
        if target_vm is None:
            return None
        return target_vm["uuid"]

    def _find_vm(self, vm_name):
        """
        仮想マシンががあることを前提とする。
        仮想マシンがない場合は例外を送出する。
        """

        vm_id = self._get_vm_id_by_vm_name(vm_name)
        if vm_id is None:
            raise Exception("vm {} is not found".format(vm_name))
        return vm_id

    def _wait_until(self, vm_id, status):
        for _i in range(0, SLEEP_COUNT):
            time.sleep(SLEEP_TIME_SEC)
            vm_info = self._mdxlib.get_vm_info(vm_id)
            logger.debug("waiting expected: {} actual: {}".format(
                status, vm_info["status"]))

            if vm_info["status"] == status:
                break
        else:
            raise MdxRestException("wait_until {} is failed".format(status))
        logger.debug("wait_until finished")


# デフォルトのresolverがIPv6のアドレスを返すが、接続できないときに以下のコードを実行する
def use_ipv4_only():
    import socket
    orig_getaddrinfo = socket.getaddrinfo

    def new_getaddrinfo(*args, **kwargs):
        responses = orig_getaddrinfo(*args, **kwargs)
        return [response
                for response in responses
                if response[0] == socket.AF_INET]
    socket.getaddrinfo = new_getaddrinfo


# 簡易テスト
def test(auth_info):
    # 仮想マシンの仕様
    pack_num = 4
    pack_type = "cpu"
    disk_size = 50
    gpu = "0"
    mdx_vm_default_template_name = "00_Ubuntu-2004-server (Recommended)"
    mdx_vm_default_catalog_id = "16a41081-a1cf-428e-90d0-a147b3aa6fc2"
    segment_name = "OCS-Developer"
    project_name = "OCS-Developer"

    vm_name = "python_test"
    ssh_shared_key_path = "/home/mdxuser/.ssh/id_rsa_mdx.pub"
    # ssh_private_key_path = "/home/mdxuser/.ssh/id_rsa_mdx"

    # loggerの設定
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    # main
    mdx = MdxResourceExt()
    mdx.login(auth_info)

    projects = mdx.get_assigned_projects()
    logger.debug(projects)

    mdx.set_current_project_by_name(project_name)
    logger.debug("current project")
    logger.debug(mdx.get_current_project())

    # vm_name = "test_01"
    # logger.debug(mdx.get_vm_info(vm_name))

    # network
    logger.debug("network_segments")
    segment_list = mdx.get_segments()
    # segment_id = segment_list[0]["uuid"]
    logger.debug(segment_list)

    for segment in segment_list:
        if segment["name"] == segment_name:
            segment_id = segment["uuid"]
            break
    else:
        raise Exception("segment {} is not found".format(segment_name))

    logger.debug("dnat")
    logger.debug(json.dumps(mdx.get_dnat(), indent=2))

    logger.debug("acl")
    # acl_ipv4_id = "461a371d-8d33-4b8f-84f3-12fe61139d0c"
    # mdx.delete_allow_acl_ipv4_info(acl_ipv4_id)
    logger.debug(json.dumps(mdx.get_allow_acl_ipv4_info(segment_id), indent=2))
    # acl_spec = {
    #     "segment": segment_id,
    #     "src_address": "153.156.73.115",
    #     "src_mask": "32",
    #     "src_port": "1111",
    #     "dst_address": "10.17.152.0",
    #     "dst_mask": "21",
    #     "dst_port": "1111",
    #     "protocol": "TCP",
    # }
    # logger.debug(mdx.add_allow_acl_ipv4_info(acl_spec))

    # logger.debug(json.dumps(mdx.get_vm_list(), indent=2))

    # logger.debug("catalog")
    # vm_catalog_list = mdx.get_vm_catalogs()
    # logger.debug(vm_catalog_list)

    # target_catalog = None
    # for catalog in vm_catalog_list:
    #     if catalog["name"].startswith(mdx_vm_default_template_name):
    #         target_catalog = catalog
    #         break
    # else:
    #     raise Exception("no catalog found: {}".format(mdx_vm_default_template_name))

    # logger.debug(target_catalog)

    # logger.debug("network_segment_summary")
    # # TODO: error: not found
    # logger.debug(mdx.get_segment_summary(segment_id))

    # 動作確認済み
    # logger.debug("project history")
    # logger.debug(mdx.get_project_history())

    # 既に仮想マシンがあったら一旦削除
    try:
        logger.debug(mdx.get_vm_info(vm_name))
#        mdx.power_off_vm(vm_name)
#        mdx.destroy_vm(vm_name)
    except Exception:
        logger.exception("init poweroff")

    with open(ssh_shared_key_path) as f:
        ssh_shared_key = f.read()

    lst = mdx.get_vm_list()
    logger.debug(json.dumps(lst, indent=2))

    # 以下変更不要
    # vm_name=vm_name,
    # os_type="Linux",
    # power_on=True,
    # project=project_id,
    vm_spec = dict(
        catalog=mdx_vm_default_catalog_id,
        template_name=mdx_vm_default_template_name,
        pack_num=pack_num,
        pack_type=pack_type,
        disk_size=disk_size,
        gpu=gpu,
        # TODO: ネットワークセグメントの名前のリストで指定させる?
        network_adapters=[
            dict(
                adapter_number=1,
                segment=segment_id
            )
        ],
        shared_key=ssh_shared_key,
        storage_network="portgroup",
    )
    info = mdx.deploy_vm(vm_name, vm_spec)

    # deploy completed
    # ssh loginができるようになるまで待つ
    logger.debug(json.dumps(info, indent=2))
    logger.debug(json.dumps(mdx.get_vm_info(vm_name), indent=2))

    # mdx.reboot_vm(vm_name)
    # mdx.power_off_vm(vm_name)
    # mdx.destroy_vm(vm_name)
    # logger.debug("project history")
    # logger.debug(json.dumps(mdx.get_project_history(), indent=2))


if __name__ == "__main__":
    # test
    username = sys.argv[1]
    password = sys.argv[2]

    # main
    auth_info = dict(
        username=username,
        password=password,
    )

    # mini test
    # mdx = MdxResourceExt()
    # mdx.login(auth_info)

    test(auth_info)
