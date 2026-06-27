public class sf_Overloads {
    public void process(Id recordId) {
        process(new List<Id>{ recordId });
    }

    public void process(List<Id> recordIds) {
        log('processing');
    }

    public void log(String message) {
        System.debug(message);
    }
}
