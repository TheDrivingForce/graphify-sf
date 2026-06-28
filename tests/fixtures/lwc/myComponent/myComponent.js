import { LightningElement, wire, api } from 'lwc';
import { getRecord } from 'lightning/uiRecordApi';
import getAccounts from '@salesforce/apex/AccountService.getAccounts';
import saveAccount from '@salesforce/apex/AccountService.saveAccount';

export default class MyComponent extends LightningElement {
    @api recordId;
    @api label;

    // @wire to a UI-API adapter (not Apex) — must NOT produce an edge.
    @wire(getRecord, { recordId: '$recordId' })
    wiredRecord;

    @wire(getAccounts, { name: 'Test' })
    wiredAccounts({ error, data }) {
        if (data) {
            this.accounts = data;
        }
    }

    handleSave() {
        // Imperative Apex call (imported, not @wire'd) -> lwc_calls edge.
        saveAccount({ acc: this.account });
    }
}
