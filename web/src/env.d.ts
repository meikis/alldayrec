declare module "*.vue" {
  import type { DefineComponent } from "vue";
  const component: DefineComponent;
  export default component;
}

// The slice of Web Bluetooth the BLE recorder uses (not in TypeScript's DOM lib; Chrome ships it).
interface BluetoothRemoteGATTCharacteristic extends EventTarget {
  value?: DataView;
  oncharacteristicvaluechanged: ((e: Event) => void) | null;
  readValue(): Promise<DataView>;
  writeValueWithResponse(v: BufferSource): Promise<void>;
  startNotifications(): Promise<BluetoothRemoteGATTCharacteristic>;
}
interface BluetoothRemoteGATTServer {
  connected: boolean;
  connect(): Promise<BluetoothRemoteGATTServer>;
  disconnect(): void;
  getPrimaryService(
    uuid: string,
  ): Promise<{ getCharacteristic(uuid: string): Promise<BluetoothRemoteGATTCharacteristic> }>;
}
interface BluetoothDevice extends EventTarget {
  name?: string;
  gatt?: BluetoothRemoteGATTServer;
}
interface Navigator {
  bluetooth: {
    requestDevice(o: { filters: { services: string[] }[]; optionalServices?: string[] }): Promise<BluetoothDevice>;
  };
}
