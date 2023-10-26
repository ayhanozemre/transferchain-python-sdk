import os
import uuid
import queue
import tempfile
import threading
import datetime
import shutil
from pathlib import Path
import grpc
from transferchain import constants
from transferchain import blockchain
from transferchain.utils import datetime_to_str
from transferchain.crypt import crypt
from transferchain.grpc_client import get_client
from transferchain.protobuf import service_pb2 as pb
from transferchain.transaction import create_transaction
from transferchain.datastructures import (
    Result, DataStorage, StorageResult, DataStorageDelete)


class Storage(object):

    def __init__(self, config):
        self.config = config

    def delete(self, user, storage_result_object):
        sender = user.random_address().Key
        sender_recipient_address = user.random_address().Key['Address']

        result_queue = queue.Queue()
        threads = []
        for slot_dict in storage_result_object.slots:
            t = threading.Thread(
                target=self._delete_slot, args=(slot_dict, result_queue))
            threads.append(t)

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        error_messages = ''
        for i in range(len(threads)):
            result = result_queue.get()
            if result.success is False:
                error_messages += result.error_messages
        if error_messages:
            return Result(success=False, error_messages=error_messages)
        tx_data = DataStorageDelete(
            UUID=storage_result_object.uuid,
            TxID=storage_result_object.txId,
            FileName=storage_result_object.filename,
            Timestamp=datetime_to_str(datetime.datetime.now()))
        tx = create_transaction(
            constants.TX_TYPE_STORAGE_DELETE, sender,
            sender_recipient_address, tx_data)
        result = blockchain.broadcast(tx)
        if result.success is False:
            return Result(
                success=False,
                error_message='Storage delete is not published on the blockchain.') # noqa
        return Result(success=True)

    def _delete_slot(self, slot_dict, result_queue):
        grpc_client = get_client()
        meta_data = [
            ("user-id", str(self.config.user_id)),
            ("user-api-token", self.config.api_token),
            ("user-api-secret", self.config.api_secret)
        ]
        slot = pb.UploadSlot(
            UUID=slot_dict.get('UUID'),
            BaseUUID=slot_dict.get('BaseUUID'),
            StorageService=slot_dict.get('StorageService'),
            Address=slot_dict.get('Address'),
            Size=slot_dict.get('Size'),
            SizeRL=slot_dict.get('SizeRL'),
            StorageCode=slot_dict.get('StorageCode'),
            userID=slot_dict.get('userID'))
        result = Result(success=True)
        try:
            grpc_client.Delete(pb.DeleteRequest(
                uuid=slot.UUID,
                StorageCode=slot.StorageCode,
                WalletID=self.config.wallet_id,
                slot=slot,
                opCode=pb.UploadOpCode.Transfer,
                UserID=self.config.user_id
            ), metadata=meta_data)
        except grpc.RpcError as e:
            error_message = 'delete error:{}'.format(e.details())
            result = Result(success=False, error_message=error_message)
        result_queue.put(result)
        return result

    def upload_single_file(self, session_id, base_uuid_map, process_uuid,
                           user, file_object, result_queue, callback):
        aes_key = crypt.generate_encrypt_key(32).encode('utf-8')
        hmac_key = crypt.generate_encrypt_key(32).encode('utf-8')

        tmp_folder = tempfile.mkdtemp()
        out_file_uuid = str(uuid.uuid4())
        out_file = Path(os.path.join(tmp_folder, out_file_uuid))
        with out_file.open(mode='wb') as outfile:
            with file_object.open(mode='rb') as infile:
                crypt.encrypt_aesctr_with_hmac(
                    infile, outfile, aes_key, hmac_key)

        file_path = str(file_object)
        file_uuid = base_uuid_map[file_path]
        meta_data = [
            ("user-id", str(self.config.user_id)),
            ("user-api-token", self.config.api_token),
            ("user-api-secret", self.config.api_secret),
            ("uuid", file_uuid),
            ("baseuuid", file_uuid),
            ("sessionid", session_id)
        ]

        sender = user.random_address().Key
        sender_recipient = user.random_address().Key

        grpc_client = get_client()
        try:
            upload_init_result = grpc_client.UploadInitV2(
                pb.UploadInitRequest(
                    fileName=file_path,
                    fileSize=file_object.stat().st_size,
                    opCode=pb.UploadOpCode.Storage,
                    userID=self.config.user_id,
                    walletID=self.config.wallet_id,
                    DeleteAfter=0,
                    senderAddress=sender['Address'],
                ), metadata=meta_data)
        except grpc.RpcError as e:
            error_message = "Grpc Error:  {}".format(e.details)
            return Result(success=False, error_message=error_message)

        out_file_desc = out_file.open(mode='rb')
        tweezers = {"total_write": 0}
        error_result = None
        for slot_index, slot in enumerate(upload_init_result.Slots):
            is_last_slot = slot_index == len(upload_init_result.Slots) - 1
            payloads = self.prepare_slot_upload_request(
                session_id=session_id,
                out_file=out_file_desc,
                slot=slot,
                is_last_slot=is_last_slot,
                file_stat=out_file.stat(),
                tweezers=tweezers
            )
            error = ""
            try:
                upload_basic_result = grpc_client.UploadBasicV4(
                    payloads, metadata=meta_data)
                status_code = upload_basic_result.statusCode
                if status_code != 1:
                    error = f"upload result is not ok. result code:{status_code}" # noqa
            except grpc.RpcError as e:
                error = e.details()
                e.cancel()
            except Exception as e:
                error = str(e)

            if error:
                error_result = Result(success=False, error_message=error,
                                      data=file_path)
                break

        out_file_info = os.stat(str(out_file))
        out_file_desc.close()
        shutil.rmtree(tmp_folder)

        if error_result:
            self.cancel_upload(
                upload_init_result.Slots, pb.UploadOpCode.Storage)
            if callback:
                callback(error_result)
            result_queue.put(error_result)
            return error_result

        upload_date = datetime.datetime.now()
        file_name = os.path.basename(file_path)
        slots = []

        for slot in upload_init_result.Slots:
            slots.append({
                'BaseUUID': slot.BaseUUID,
                'UUID': slot.UUID,
                'StorageService': slot.StorageService,
                'Address': slot.Address,
                'Size': slot.Size,
                'SizeRL': slot.SizeRL,
                'StorageCode': slot.StorageCode,
                'userID': slot.userID})

        tx_data = DataStorage(
            UUID=upload_init_result.BaseUUID,
            FileName=file_name,
            Size=out_file_info.st_size,
            Slots=slots,
            KeyAES=aes_key.decode("utf-8"),
            KeyHMAC=hmac_key.decode("utf-8"),
            StorageCode=pb.UploadOpCode.Storage,
            Address=upload_init_result.Address,
            UploadDate=datetime_to_str(upload_date))
        tx = create_transaction(
            constants.TX_TYPE_STORAGE, sender,
            sender_recipient['Address'], tx_data)
        broadcast_result = blockchain.broadcast(tx)
        if broadcast_result.success is False:
            error_result = Result(success=False, error_message='The storage is not published on the blockchain.') # noqa
            self.cancel_upload(
                upload_init_result.Slots, pb.UploadOpCode.Storage)
            if callback:
                callback(error_result)
            result_queue.put(error_result)
            return error_result

        storage = StorageResult(
            txId=tx['tx_id'],
            filename=file_name,
            slots=slots,
            keyAES=aes_key.decode("utf-8"),
            keyHMAC=hmac_key.decode("utf-8"),
            uuid=upload_init_result.BaseUUID,
            senderAddress=sender['Address'],
            recipientAddress=sender['Address'],
            size=out_file_info.st_size,
            uploadDate=datetime_to_str(upload_date),
            storage_code=upload_init_result.StorageCode,
            address=upload_init_result.Address)
        result = Result(success=True, data=storage)
        if callback:
            callback(result)
        result_queue.put(result)
        return result

    def upload(self, user, files, callback=None):
        assert len(files) <= constants.STORAGE_MAX_FILE_COUNT, \
            'file count exceeded'

        if callback is not None:
            assert callable(callback), 'callback is not a function'

        file_objects = []
        total_file_size = 0
        for file_path in files:
            file_object = Path(file_path)
            if not file_object.exists():
                return Result(success=False, error_message='file does not exist')  # noqa
            file_objects.append(file_object)
            total_file_size += file_object.stat().st_size

        result_queue = queue.Queue()
        process_uuid = str(uuid.uuid4())
        threads = []

        meta_data = [
            ("user-id", str(self.config.user_id)),
            ("user-api-token", self.config.api_token),
            ("user-api-secret", self.config.api_secret)
        ]
        grpc_client = get_client()
        try:
            init_result = grpc_client.StorageInitV2(
                pb.StorageInitRequest(
                    TotalSize=total_file_size,
                    Paths=files,
                    OpCode=pb.UploadOpCode.Storage,
                    UserID=self.config.user_id,
                    WalletID=self.config.wallet_id,
                    notes="",
                    UID="",
                ), metadata=meta_data)
        except grpc.RpcError as e:
            error_message = "storage init request error: {}".format(
                e.details())
            return Result(success=False, error_message=error_message)

        for file_object in file_objects:
            t = threading.Thread(
                target=self.upload_single_file, args=(
                    init_result.SessionID,
                    init_result.BaseUUIDs,
                    process_uuid,
                    user,
                    file_object,
                    result_queue,
                    callback))
            threads.append(t)

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        results = []
        for result in range(len(threads)):
            results.append(result_queue.get())

        try:
            grpc_client.StorageFinishV2(
                pb.StorageFinishRequest(
                    SessionID=init_result.SessionID,
                    UserID=self.config.user_id,
                    WalletID=self.config.wallet_id), metadata=meta_data)
        except grpc.RpcError as e:
            # cancel uploads
            for result in results:
                slots = result.data['slots']
                self.cancel_upload(slots, pb.UploadOpCode.Storage)
            error_message = "storage finish request error: {}".format(
                e.details())
            return Result(success=False, error_message=error_message)
        return Result(success=True, data=results)

    def prepare_slot_upload_request(
            self, session_id, out_file, slot,
            is_last_slot, file_stat, tweezers):
        chunk_size = constants.UPLOAD_CHUNK_SIZE

        slot_upload_size = 0
        total_read = 0
        buff_size = chunk_size
        while True:
            if total_read + chunk_size > slot.Size:
                if is_last_slot:
                    buff_size = file_stat.st_size - tweezers['total_write']
                else:
                    buff_size = slot.Size - total_read

            data = out_file.read(buff_size)
            if not data:
                break

            total_read += buff_size
            tweezers['total_write'] += buff_size
            slot_upload_size += buff_size
            payload = pb.UploadV3Request(
                Chunk=data,
                Slot=slot,
                LastSlot=is_last_slot,
            )

            yield payload
            if total_read >= slot.Size:
                total_read = 0
                break

    def cancel_upload(self, slots, op_code):
        grpc_client = get_client()
        meta_data = [
            ("user-id", str(self.config.user_id)),
            ("user-api-token", self.config.api_token),
            ("user-api-secret", self.config.api_secret)
        ]
        for slot_dict in slots:
            slot = pb.UploadSlot(
                UUID=slot_dict.get('UUID'),
                BaseUUID=slot_dict.get('BaseUUID'),
                StorageService=slot_dict.get('StorageService'),
                Address=slot_dict.get('Address'),
                Size=slot_dict.get('Size'),
                SizeRL=slot_dict.get('SizeRL'),
                StorageCode=slot_dict.get('StorageCode'),
                userID=slot_dict.get('userID'))
            try:
                grpc_client.DeleteV2(pb.DeleteRequest(
                    uuid=slot.UUID,
                    StorageCode=slot.StorageCode,
                    WalletID=self.config.wallet_id,
                    slot=slot,
                    opCode=op_code,
                    UserID=self.config.user_id
                ), metadata=meta_data)
            except grpc.RpcError as e:
                error_message = 'cancel upload error:{}'.format(e.details())
                return Result(success=False, error_message=error_message)
        return Result(success=True)
